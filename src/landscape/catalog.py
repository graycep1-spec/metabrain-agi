"""Каталог типовых коннекторов: группы, способ входа и политика по ролям.

Каталог — витрина и политика, а не готовый адаптер. Фактическое подключение к системе
всё равно проходит согласие куратора и профиль Adapter; для протокольных систем
(AD/RDP/SSH/WinRM) нужен отдельный адаптер и техническая учётная запись.
"""

from __future__ import annotations

from typing import Any


CONNECTOR_GROUPS: list[dict[str, Any]] = [
    {
        "id": "it",
        "title": "ИТ-инфраструктура",
        "description": "Доступ к серверам, каталогу и платформе. Только для ИТ и куратора, с отдельным согласием.",
        "connectors": [
            {
                "id": "active-directory", "name": "Active Directory", "kind": "LDAP",
                "auth": "Техническая учётная запись LDAP (bind), только чтение",
                "secret_label": "Логин и пароль сервисной учётной записи",
                "allowed_roles": ["Куратор", "ИТ-администратор"],
                "denied_roles": ["Менеджер", "Финансы", "Сотрудник"],
                "approval": True, "source": "scout",
                "note": "Не давайте доменному администратору; нужна отдельная read-only учётка.",
            },
            {
                "id": "rdp", "name": "RDP", "kind": "RDP",
                "auth": "Техническая учётная запись Windows + доступ к хосту",
                "secret_label": "Логин, пароль и адрес хоста",
                "allowed_roles": ["Куратор", "ИТ-администратор"],
                "denied_roles": ["Менеджер", "Финансы", "Сотрудник"],
                "approval": True, "source": "standard",
                "note": "Только чтение по умолчанию; запись — отдельный класс действия L1/L2.",
            },
            {
                "id": "ssh", "name": "SSH", "kind": "SSH",
                "auth": "SSH-ключ или сервисная учётная запись",
                "secret_label": "Приватный ключ или логин/пароль",
                "allowed_roles": ["Куратор", "ИТ-администратор"],
                "denied_roles": ["Менеджер", "Финансы", "Сотрудник"],
                "approval": True, "source": "scout",
                "note": "Ключ хранится в vault; не переиспользуйте личный ключ администратора.",
            },
            {
                "id": "winrm", "name": "WinRM", "kind": "WinRM",
                "auth": "Техническая учётная запись Windows с правом WinRM",
                "secret_label": "Логин, пароль и адрес хоста",
                "allowed_roles": ["Куратор", "ИТ-администратор"],
                "denied_roles": ["Менеджер", "Финансы", "Сотрудник"],
                "approval": True, "source": "scout",
                "note": "Требуется настроенный WinRM и отдельная учётная запись.",
            },
            {
                "id": "vmware", "name": "VMware vSphere", "kind": "vSphere API",
                "auth": "Сервисная учётная запись с ролью read-only",
                "secret_label": "Логин и пароль сервисной учётной записи",
                "allowed_roles": ["Куратор", "ИТ-администратор"],
                "denied_roles": ["Менеджер", "Финансы", "Сотрудник"],
                "approval": True, "source": "standard", "note": "",
            },
            {
                "id": "kubernetes", "name": "Kubernetes", "kind": "Kubernetes API",
                "auth": "ServiceAccount token с read-only RBAC",
                "secret_label": "Bearer token ServiceAccount",
                "allowed_roles": ["Куратор", "ИТ-администратор"],
                "denied_roles": ["Менеджер", "Финансы", "Сотрудник"],
                "approval": True, "source": "standard", "note": "",
            },
        ],
    },
    {
        "id": "platforms",
        "title": "Платформы",
        "description": "Почта, календарь, задачи и совместная работа. Доступ выдаётся по OAuth-токену или сервисной учётке.",
        "connectors": [
            {
                "id": "outlook", "name": "Outlook.com / Microsoft 365", "kind": "Microsoft Graph",
                "auth": "OAuth2 (client credentials или delegated token)",
                "secret_label": "Tenant ID, Client ID и Client Secret",
                "allowed_roles": ["Куратор", "Офис-менеджер", "Менеджер"],
                "denied_roles": ["ИТ-администратор", "Финансы"],
                "approval": True, "source": "scout",
                "note": "Почта и календарь — персональные данные; нужен минимальный scope.",
            },
            {
                "id": "google-workspace", "name": "Google Workspace", "kind": "Google API",
                "auth": "OAuth2 service account или delegated token",
                "secret_label": "Service account JSON или OAuth-токен",
                "allowed_roles": ["Куратор", "Офис-менеджер", "Менеджер"],
                "denied_roles": ["ИТ-администратор", "Финансы"],
                "approval": True, "source": "standard", "note": "",
            },
            {
                "id": "telegram", "name": "Telegram", "kind": "Bot API",
                "auth": "Токен отдельного бота",
                "secret_label": "Bot token",
                "allowed_roles": ["Куратор"],
                "denied_roles": ["ИТ-администратор", "Менеджер", "Финансы", "Сотрудник"],
                "approval": True, "source": "standard", "note": "Не используйте токен TSN-бота.",
            },
            {
                "id": "jira", "name": "Jira", "kind": "Jira API",
                "auth": "API-токен или OAuth2",
                "secret_label": "Email + API token",
                "allowed_roles": ["Куратор", "ИТ-администратор", "Менеджер"],
                "denied_roles": ["Финансы"],
                "approval": False, "source": "standard", "note": "",
            },
            {
                "id": "confluence", "name": "Confluence", "kind": "Confluence API",
                "auth": "API-токен или OAuth2",
                "secret_label": "Email + API token",
                "allowed_roles": ["Куратор", "ИТ-администратор", "Менеджер"],
                "denied_roles": ["Финансы"],
                "approval": False, "source": "standard", "note": "",
            },
            {
                "id": "gitlab", "name": "GitLab", "kind": "GitLab API",
                "auth": "Personal/Project access token (read-only)",
                "secret_label": "Access token",
                "allowed_roles": ["Куратор", "ИТ-администратор"],
                "denied_roles": ["Менеджер", "Финансы", "Сотрудник"],
                "approval": False, "source": "standard", "note": "",
            },
        ],
    },
    {
        "id": "business",
        "title": "Бизнес-системы",
        "description": "CRM, склад, цены, финансы. Доступ — по API-токену или сервисной учётной записи; политика зависит от роли.",
        "connectors": [
            {
                "id": "bitrix24", "name": "Bitrix24", "kind": "CRM",
                "auth": "Webhook/входящий токен или OAuth2",
                "secret_label": "Webhook URL или OAuth-токен",
                "allowed_roles": ["Куратор", "Менеджер", "Финансы"],
                "denied_roles": ["ИТ-администратор", "Сотрудник"],
                "approval": False, "source": "standard", "note": "",
            },
            {
                "id": "amocrm", "name": "amoCRM", "kind": "CRM",
                "auth": "OAuth2 integration token",
                "secret_label": "Client ID/Secret и refresh token",
                "allowed_roles": ["Куратор", "Менеджер"],
                "denied_roles": ["ИТ-администратор", "Финансы", "Сотрудник"],
                "approval": False, "source": "standard", "note": "",
            },
            {
                "id": "1c", "name": "1С", "kind": "ERP",
                "auth": "Сервисная учётная запись 1С или HTTP-сервис",
                "secret_label": "Логин/пароль или токен HTTP-сервиса",
                "allowed_roles": ["Куратор", "Финансы"],
                "denied_roles": ["Менеджер", "ИТ-администратор", "Сотрудник"],
                "approval": True, "source": "scout",
                "note": "Финансовый контур; запись — только L1/L2 по согласию.",
            },
            {
                "id": "sap", "name": "SAP", "kind": "ERP",
                "auth": "Техническая учётная запись SAP (OData/BAPI)",
                "secret_label": "Пользователь, пароль и мандант",
                "allowed_roles": ["Куратор", "Финансы"],
                "denied_roles": ["Менеджер", "ИТ-администратор", "Сотрудник"],
                "approval": True, "source": "standard", "note": "",
            },
            {
                "id": "warehouse", "name": "Склад", "kind": "WMS",
                "auth": "API-токен WMS",
                "secret_label": "Bearer token",
                "allowed_roles": ["Куратор", "Менеджер", "Финансы"],
                "denied_roles": ["ИТ-администратор"],
                "approval": False, "source": "standard", "note": "",
            },
            {
                "id": "pricing", "name": "Прайс и маржа", "kind": "Pricing",
                "auth": "API-токен или read-only БД",
                "secret_label": "Токен или строка подключения",
                "allowed_roles": ["Куратор", "Менеджер", "Финансы"],
                "denied_roles": ["ИТ-администратор", "Сотрудник"],
                "approval": False, "source": "standard", "note": "",
            },
        ],
    },
]


def catalog() -> list[dict[str, Any]]:
    return CONNECTOR_GROUPS


def connector(connector_id: str) -> dict[str, Any] | None:
    for group in CONNECTOR_GROUPS:
        for item in group["connectors"]:
            if item["id"] == connector_id:
                return {**item, "group": group["id"], "group_title": group["title"]}
    return None
