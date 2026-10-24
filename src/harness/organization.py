"""Профиль организации (ADR-035): отрасль, размер, KPI — одна строка на компанию.

Профиль наследуется ролями: KPI подмешивается в цели новых сотрудников
(каталог — детерминированно, свободное описание — контекстом для модели),
а процессы отрасли показываются в каталоге первыми.
"""

from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import Connection


INDUSTRIES = {
    "": "не указана",
    "sales": "Продажи и дистрибуция",
    "services": "Услуги",
    "manufacturing": "Производство",
    "logistics": "Логистика",
    "retail": "Розница",
    "it": "ИТ и диджитал",
}

SIZES = {
    "": "не указан",
    "micro": "до 15 человек",
    "small": "15–100 человек",
    "medium": "100–250 человек",
}

MAX_KPI = 500


class OrgStore:
    def __init__(self, connection: Connection):
        self._c = connection
        with connection.transaction():
            connection.execute(
                Path(__file__).with_name("organization.sql").read_text(encoding="utf-8"),
                prepare=False,
            )

    def get(self, company_id: UUID) -> dict[str, Any]:
        with self._c.transaction():
            row = self._c.execute(
                "SELECT industry, size, kpi, metabrain_enabled FROM org_settings WHERE company_id = %s",
                (company_id,),
            ).fetchone()
        industry, size, kpi, metabrain_enabled = row if row else ("", "", "", True)
        return {
            "industry": industry, "industry_label": INDUSTRIES.get(industry, industry),
            "size": size, "size_label": SIZES.get(size, size),
            "kpi": kpi, "metabrain_enabled": bool(metabrain_enabled),
        }

    def set_metabrain(self, company_id: UUID, *, enabled: bool, updated_by: UUID | None) -> dict[str, Any]:
        with self._c.transaction():
            self._c.execute(
                """
                INSERT INTO org_settings (company_id, metabrain_enabled, updated_at, updated_by)
                VALUES (%s, %s, now(), %s)
                ON CONFLICT (company_id) DO UPDATE SET
                    metabrain_enabled = EXCLUDED.metabrain_enabled,
                    updated_at = EXCLUDED.updated_at,
                    updated_by = EXCLUDED.updated_by
                """,
                (company_id, bool(enabled), updated_by),
            )
        return self.get(company_id)

    def put(self, company_id: UUID, *, industry: Any, size: Any, kpi: Any,
            updated_by: UUID | None) -> dict[str, Any]:
        industry = (industry or "").strip() if isinstance(industry, str) else ""
        size = (size or "").strip() if isinstance(size, str) else ""
        kpi = (kpi or "").strip() if isinstance(kpi, str) else ""
        if industry not in INDUSTRIES:
            raise ValueError("unknown industry")
        if size not in SIZES:
            raise ValueError("unknown size")
        if len(kpi) > MAX_KPI:
            raise ValueError(f"kpi must be at most {MAX_KPI} characters")
        with self._c.transaction():
            self._c.execute(
                """
                INSERT INTO org_settings (company_id, industry, size, kpi, updated_at, updated_by)
                VALUES (%s, %s, %s, %s, now(), %s)
                ON CONFLICT (company_id) DO UPDATE SET
                    industry = EXCLUDED.industry, size = EXCLUDED.size, kpi = EXCLUDED.kpi,
                    updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by
                """,
                (company_id, industry, size, kpi, updated_by),
            )
        return self.get(company_id)


def role_context(org: dict[str, Any]) -> str:
    """Контекст организации для модели при драфте роли из свободного описания."""
    parts = []
    if org.get("industry"):
        parts.append(f"отрасль — {org['industry_label']}")
    if org.get("size"):
        parts.append(f"размер — {org['size_label']}")
    if org.get("kpi"):
        parts.append(f"ключевые KPI — {org['kpi']}")
    return ("\n\nКонтекст организации: " + "; ".join(parts) +
            ". Учти это в целях и границах роли.") if parts else ""
