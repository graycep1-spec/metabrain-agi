import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx


class C1API(Protocol):
    async def call(
        self,
        operation: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class C1APIError(RuntimeError):
    def __init__(self, *, status_code: int, code: str):
        self.status_code = status_code
        self.code = code
        super().__init__(code)


class HTTPC1API:
    def __init__(
        self,
        base_url: str,
        *,
        api_token: str,
        client: httpx.AsyncClient | None = None,
    ):
        if not api_token.strip():
            raise ValueError("C-1 API token is required")
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None

    async def call(
        self,
        operation: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        try:
            response = await self._client.post(
                f"{self._base_url}/v1/operations/{operation}",
                json=dict(payload),
                headers={"Authorization": f"Bearer {self._api_token}"},
            )
        except httpx.RequestError as error:
            raise C1APIError(status_code=502, code="api_unavailable") from error
        try:
            body = response.json()
        except ValueError as error:
            raise C1APIError(
                status_code=502,
                code="invalid_api_response",
            ) from error
        if response.is_error:
            code = body.get("error") if isinstance(body, Mapping) else None
            if not code and isinstance(body, Mapping):
                detail = body.get("detail")
                if isinstance(detail, Mapping):
                    code = detail.get("error")
            raise C1APIError(
                status_code=response.status_code,
                code=str(code or "api_error"),
            )
        if not isinstance(body, Mapping):
            raise C1APIError(status_code=502, code="invalid_api_response")
        return body

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass(frozen=True)
class InlineButton:
    text: str
    callback_data: str


@dataclass(frozen=True)
class TelegramReply:
    text: str
    buttons: tuple[InlineButton, ...] = ()


class TelegramAdapter:
    def __init__(self, api: C1API):
        self._api = api

    async def handle_message(
        self,
        *,
        telegram_user_id: int,
        text: str,
    ) -> TelegramReply:
        channel_user_id = self._channel_user_id(telegram_user_id)
        content = text.strip()
        if not content:
            return TelegramReply("Отправьте команду или вопрос.")

        try:
            if content.startswith("/"):
                command, _, argument = content.partition(" ")
                command = command.split("@", 1)[0].lower()
                argument = argument.strip()
                if command == "/start":
                    return await self._start(channel_user_id, argument)
                if command == "/role":
                    if not argument:
                        return TelegramReply("Использование: /role <описание вашей роли>")
                    return await self._draft_role(channel_user_id, argument)
                if command == "/invite":
                    return await self._invite(channel_user_id)
                if command == "/connect":
                    return await self._connect(channel_user_id, argument)
                if command == "/map":
                    return await self._map(channel_user_id)
                return TelegramReply("Неизвестная команда.")

            if content.startswith("@"):
                return await self._ask_mentioned(channel_user_id, content[1:])
            return await self._ask(channel_user_id, content)
        except C1APIError as error:
            return self._api_error_reply(error)

    async def handle_callback(
        self,
        *,
        telegram_user_id: int,
        callback_data: str,
    ) -> TelegramReply:
        channel_user_id = self._channel_user_id(telegram_user_id)
        parts = callback_data.split(":")
        if len(parts) == 2 and parts[0] == "register":
            if parts[1] == "decline":
                return TelegramReply("Регистрация отменена.")
            try:
                invite = "" if parts[1] == "-" else parts[1]
                payload: dict[str, object] = {
                    "channel": "telegram",
                    "channel_user_id": channel_user_id,
                    "external_llm_consent": True,
                }
                if invite:
                    payload["company_invite"] = invite
                else:
                    payload["company_name"] = f"Компания Telegram {channel_user_id}"
                result = await self._api.call("person.register", payload)
                invite_link = result.get("invite_link")
                text = "Регистрация завершена. Ваш харнесс готов."
                if invite_link:
                    text += f"\nПриглашение в компанию: {invite_link}"
                return TelegramReply(text)
            except C1APIError as error:
                return self._api_error_reply(error)
        if len(parts) != 3 or parts[0] != "proposal":
            return TelegramReply("Не удалось распознать это решение.")

        _, proposal_id, decision = parts
        try:
            UUID(proposal_id)
        except ValueError:
            return TelegramReply("Некорректный идентификатор предложения.")
        if decision not in {"approve", "return", "incident"}:
            return TelegramReply("Неизвестный вариант решения.")

        try:
            result = await self._api.call(
                "proposal.decide",
                {
                    "channel_user_id": channel_user_id,
                    "proposal_id": proposal_id,
                    "decision": decision,
                    "surface": "telegram",
                },
            )
        except C1APIError as error:
            if error.status_code == 403:
                return TelegramReply("У вас нет прав решать это предложение.")
            if error.status_code == 409 and error.code == "already_decided":
                return await self._decided_status(channel_user_id, proposal_id)
            return self._api_error_reply(error)

        status = {
            "approved": "подтверждено",
            "returned": "возвращено на доработку",
            "incident": "отмечено как инцидент",
        }.get(str(result.get("status")), "решение принято")
        return TelegramReply(f"Решение записано: {status}.")

    async def _start(
        self,
        channel_user_id: str,
        invite: str,
    ) -> TelegramReply:
        token = invite if invite else "-"
        return TelegramReply(
            "Для регистрации нужно согласие на обработку запросов внешней "
            "LLM-моделью. Продолжая, вы соглашаетесь.",
            (
                InlineButton("Согласен", f"register:{token}"),
                InlineButton("Отказаться", "register:decline"),
            ),
        )

    async def _draft_role(
        self,
        channel_user_id: str,
        role_text: str,
    ) -> TelegramReply:
        result = await self._api.call(
            "employee.draft",
            {
                "channel_user_id": channel_user_id,
                "kind": "twin",
                "role_text": role_text,
            },
        )
        proposal_id = result.get("proposal_id")
        buttons = self._decision_buttons(proposal_id)
        return TelegramReply(
            "Черновик роли создан и отправлен куратору на подтверждение.",
            buttons,
        )

    async def _invite(self, channel_user_id: str) -> TelegramReply:
        result = await self._api.call(
            "company.invite",
            {"channel_user_id": channel_user_id},
        )
        invite_link = result.get("invite_link")
        if not isinstance(invite_link, str) or not invite_link:
            return TelegramReply("Не удалось создать приглашение.")
        return TelegramReply(f"Ссылка для приглашения в компанию: {invite_link}")

    async def _connect(
        self,
        channel_user_id: str,
        argument: str,
    ) -> TelegramReply:
        if not argument or len(argument.split()) != 1:
            return TelegramReply(
                "Использование: /connect <система>. Доступ добавьте в веб-кабинете; "
                "не отправляйте секреты в чат."
            )
        result = await self._api.call(
            "landscape.connect.start",
            {"channel_user_id": channel_user_id, "system": argument},
        )
        return TelegramReply(
            "Чтобы безопасно подключить систему, откройте веб-кабинет и добавьте "
            f"доступ там: {result.get('cabinet_url', '')}. Не отправляйте секреты в чат."
        )

    async def _map(self, channel_user_id: str) -> TelegramReply:
        result = await self._api.call(
            "landscape.map",
            {"channel_user_id": channel_user_id},
        )
        landscape = result.get("map", {})
        rendered = json.dumps(landscape, ensure_ascii=False, sort_keys=True)
        return TelegramReply(f"Карта ландшафта:\n{rendered}")

    async def _ask(self, channel_user_id: str, text: str) -> TelegramReply:
        return await self._ask_employee(channel_user_id, text)

    async def _ask_mentioned(self, channel_user_id: str, mention_and_text: str) -> TelegramReply:
        response = await self._api.call(
            "employee.list",
            {"channel_user_id": channel_user_id},
        )
        employees = response.get("employees")
        if not isinstance(employees, Sequence) or isinstance(employees, (str, bytes)):
            return TelegramReply("Не удалось получить список сотрудников.")

        remaining = mention_and_text.lstrip()
        matches: list[tuple[str, str]] = []
        for employee in employees:
            if not isinstance(employee, Mapping) or employee.get("status") != "active":
                continue
            title = employee.get("title")
            employee_id = employee.get("employee_id")
            if not isinstance(title, str) or not title.strip() or not isinstance(employee_id, str):
                continue
            normalized_title = title.strip()
            if remaining[:len(normalized_title)].casefold() == normalized_title.casefold() and (
                len(remaining) == len(normalized_title)
                or remaining[len(normalized_title)].isspace()
            ):
                matches.append((normalized_title, employee_id))

        if not matches:
            return TelegramReply("Не нашёл активного сотрудника с таким названием.")
        longest_title_length = max(len(title) for title, _ in matches)
        matches = [(title, employee_id) for title, employee_id in matches if len(title) == longest_title_length]
        if len(matches) != 1:
            return TelegramReply("Нашёл несколько сотрудников с таким названием; уточните упоминание.")

        title, employee_id = matches[0]
        question = remaining[len(title):].strip()
        if not question:
            return TelegramReply(f"Укажите вопрос после упоминания @{title}.")
        return await self._ask_employee(channel_user_id, question, employee_id=employee_id)

    async def _ask_employee(
        self,
        channel_user_id: str,
        text: str,
        *,
        employee_id: str | None = None,
    ) -> TelegramReply:
        payload: dict[str, object] = {"channel_user_id": channel_user_id, "text": text}
        if employee_id is not None:
            payload["employee_id"] = employee_id
        result = await self._api.call(
            "ask",
            payload,
        )
        answer = result.get("answer")
        sources = result.get("sources")
        score_ref = result.get("score_ref")
        if (
            not isinstance(answer, str)
            or not answer.strip()
            or not isinstance(sources, Sequence)
            or isinstance(sources, (str, bytes))
            or not sources
            or not isinstance(score_ref, str)
            or not score_ref.strip()
        ):
            return TelegramReply(
                "Ответ заблокирован: отсутствуют обязательные источники "
                "или подтверждение валидатора."
            )

        lines = []
        for source in sources:
            if isinstance(source, Mapping):
                ref = source.get("object_ref") or source.get("ref") or source.get("source_id") or ""
                lines.append(" ".join(part for part in ("—", source.get("system", ""), ref) if part))
        source_text = "\n".join(lines) or json.dumps(sources, ensure_ascii=False, sort_keys=True)
        return TelegramReply(
            f"{answer}\n\nИсточники:\n{source_text}\nПроверка валидатора: {score_ref}"
        )

    async def _decided_status(
        self,
        channel_user_id: str,
        proposal_id: str,
    ) -> TelegramReply:
        result = await self._api.call(
            "proposal.list",
            {"channel_user_id": channel_user_id},
        )
        proposals = result.get("proposals", ())
        if isinstance(proposals, Sequence) and not isinstance(
            proposals, (str, bytes)
        ):
            for proposal in proposals:
                if (
                    isinstance(proposal, Mapping)
                    and str(proposal.get("proposal_id")) == proposal_id
                ):
                    status = proposal.get("status", "решено")
                    return TelegramReply(
                        f"Предложение уже решено. Текущий статус: {status}."
                    )
        return TelegramReply("Предложение уже решено; обновите его статус в кабинете.")

    @staticmethod
    def _decision_buttons(proposal_id: object) -> tuple[InlineButton, ...]:
        try:
            normalized_id = str(UUID(str(proposal_id)))
        except (ValueError, TypeError, AttributeError):
            return ()
        return (
            InlineButton("Подтвердить", f"proposal:{normalized_id}:approve"),
            InlineButton("Вернуть", f"proposal:{normalized_id}:return"),
            InlineButton("Инцидент", f"proposal:{normalized_id}:incident"),
        )

    @staticmethod
    def _channel_user_id(telegram_user_id: int) -> str:
        if not isinstance(telegram_user_id, int) or telegram_user_id <= 0:
            raise ValueError("Telegram user id must be a positive integer")
        return str(telegram_user_id)

    @staticmethod
    def _api_error_reply(error: C1APIError) -> TelegramReply:
        messages = {
            "external_llm_consent_required": "Для регистрации нужно сначала дать согласие.",
            "invalid_invite": "Ссылка приглашения недействительна или истекла.",
            "registration_required": "Сначала выполните /start и завершите регистрацию.",
            "forbidden_harness": "У вас нет доступа к этому харнессу.",
            "not_curator": "У вас нет прав решать это предложение.",
            "already_decided": "Это предложение уже решено.",
            "answer_blocked": "Ответ не выдан: его не удалось подтвердить источниками. "
                              "Проверьте, что системы подключены, и спросите иначе.",
            "employee_busy": "Сотрудник ещё отвечает на прошлый вопрос. Подождите немного.",
            "not_found": "Пока нет активного сотрудника: опишите роль командой /role и подтвердите паспорт.",
            "budget_exceeded": "Достигнут лимит расходов на модели. Обратитесь к администратору.",
            "workforce_unavailable": "Сотрудник сейчас недоступен. Попробуйте позже.",
        }
        return TelegramReply(
            messages.get(error.code, "Не удалось выполнить запрос. Попробуйте позже.")
        )


def create_router(adapter: TelegramAdapter):
    from aiogram import Router
    from aiogram.types import (
        CallbackQuery,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Message,
    )

    router = Router(name="metabrain-telegram")

    def keyboard_for(reply: TelegramReply) -> InlineKeyboardMarkup | None:
        if not reply.buttons:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button.text,
                        callback_data=button.callback_data,
                    )
                    for button in reply.buttons
                ]
            ]
        )

    @router.message()
    async def handle_incoming_message(message: Message) -> None:
        if message.from_user is None or message.text is None:
            return
        reply = await adapter.handle_message(
            telegram_user_id=message.from_user.id,
            text=message.text,
        )
        await message.answer(reply.text, reply_markup=keyboard_for(reply))

    @router.callback_query()
    async def handle_incoming_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        reply = await adapter.handle_callback(
            telegram_user_id=callback.from_user.id,
            callback_data=callback.data,
        )
        await callback.answer()
        if callback.message is not None:
            await callback.message.edit_text(
                reply.text,
                reply_markup=keyboard_for(reply),
            )

    return router
