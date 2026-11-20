"""
Контракт данных тикета поддержки:

1. Поля модели тикета
    - Обязательные при создании
        title: str (мин. длина - 1 символ)
    - Необязательные при создании
        description: str | None (по умолч. None)
        priority: Priority (по умолч. Priority.normal)
        assignee: srt | None (по умолч. None)
    - Вычисляемые сервером
        id: int (автоинкрементный уникальный идентификатор)
        status: TicketStatus (начальное значение всегда open)
        created_at: datetime (UTC-время создания)

2. Допустимые значения перечислений
    - priority: "low", "normal", "high", "urgent"
    - status: "open", "in_progress", "resolved", "closed"

3. Возможности частичного изменения (PATCH /tickets/{ticket_id})
   - Разрешено менять (TicketUpdate): description, priority, assignee, status
   - Запрещено менять: id, title, created_at (отсутствуют в модели TicketUpdate)

4. Разрешенные переходы статусов (ALLOWED_TRANSITIONS)
   - open        -> { in_progress }
   - in_progress -> { resolved, open }
   - resolved    -> { closed, in_progress }
   - closed      -> переходы отсутствуют (терминальный статус)

5. Сортировка и пагинация
   - Сортировка по умолчанию: sort_by="created_at", order="desc"
   - Поля сортировки: created_at, priority (low < normal < high < urgent)
   - Порядок сортировки: asc, desc
   - Пагинация: page по умолчанию 1 (ge=1)
   - page_size: по умолчанию 5 (Settings.default_page_size), максимум 20 (Settings.max_page_size)

6. Коды возврата и ошибки
    - 200 OK: успешное получение или обноление тикета
    - 201 Created: успешное создание тикета
    - 204 No Content: успешное удаление тикета
    - 404 Not Found: тикет с переданным id не найден (TicketNotFoundError)
    - 409 Conflict: недопустимый переход статуса (InvalidStatusTransitionError)
    - 422 Unprocessable Entity: ошибка валидации входящящих данных FastAPI/Pydantic
"""

from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# --- Конфиг ---------------------------------------------------------------

class Settings(BaseModel):
    default_page_size: int = 5
    max_page_size: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()


# --- Исключения -------------------------------------------------------------

class HelpDeskError(Exception):
    """Базовое исключение сервиса."""


class TicketNotFoundError(HelpDeskError):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(f"Тикет {ticket_id} не найден")


class InvalidStatusTransitionError(HelpDeskError):
    def __init__(self, current: "TicketStatus", target: "TicketStatus") -> None:
        self.current = current
        self.target = target
        super().__init__(f"Переход {current} -> {target} запрещён")


# --- Схемы ------------------------------------------------------------------

class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class TicketStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


ALLOWED_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.open: {TicketStatus.in_progress},
    TicketStatus.in_progress: {TicketStatus.resolved, TicketStatus.open},
    TicketStatus.resolved: {TicketStatus.closed, TicketStatus.in_progress},
    TicketStatus.closed: set(),
}

PRIORITY_WEIGHTS: dict[Priority, int] = {
    Priority.low: 1,
    Priority.normal: 2,
    Priority.high: 3,
    Priority.urgent: 4,
}


class TicketCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str | None = None
    priority: Priority = Priority.normal
    assignee: str | None = None


class TicketUpdate(BaseModel):
    description: str | None = None
    priority: Priority | None = None
    assignee: str | None = None
    status: TicketStatus | None = None


class Ticket(BaseModel):
    id: int
    title: str
    description: str | None
    priority: Priority
    status: TicketStatus
    assignee: str | None
    created_at: datetime


class TicketPage(BaseModel):
    items: list[Ticket]
    total: int
    page: int
    page_size: int


# --- Репозиторий --------------------------------------------------------------

class TicketRepository:
    """Хранит тикеты в памяти процесса."""

    def __init__(self) -> None:
        self._tickets: dict[int, Ticket] = {}
        self._next_id = 1

    def add(self, ticket: Ticket) -> Ticket:
        self._tickets[ticket.id] = ticket
        return ticket

    def get(self, ticket_id: int) -> Ticket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(ticket_id)
        return ticket

    def list_all(self) -> list[Ticket]:
        return list(self._tickets.values())

    def update(self, ticket: Ticket) -> Ticket:
        if ticket.id not in self._tickets:
            raise TicketNotFoundError(ticket.id)
        self._tickets[ticket.id] = ticket
        return ticket

    def delete(self, ticket_id: int) -> None:
        if ticket_id not in self._tickets:
            raise TicketNotFoundError(ticket_id)
        del self._tickets[ticket_id]
        


    def next_id(self) -> int:
        ticket_id = self._next_id
        self._next_id += 1
        return ticket_id


# --- Сервис ------------------------------------------------------------------

class TicketService:
    def __init__(self, repository: TicketRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def create_ticket(self, payload: TicketCreate) -> Ticket:
        ticket = Ticket(
            id=self._repository.next_id(),
            title=payload.title,
            description=payload.description,
            priority=payload.priority,
            status=TicketStatus.open,
            assignee=payload.assignee,
            created_at=datetime.now(timezone.utc),
        )
        return self._repository.add(ticket)

    def get_ticket(self, ticket_id: int) -> Ticket:
        return self._repository.get(ticket_id)

    def list_tickets(
        self,
        sort_by: str,
        order: str,
        page: int,
        page_size: int | None,
    ) -> TicketPage:
        # 1. Размер страницы: дефолт из конфига, сверху ограничен максимумом
        limit = self._settings.default_page_size if page_size is None else page_size
        effective_page_size = min(limit, self._settings.max_page_size)

        # 2. Берём все тикеты из репо
        all_tickets = self._repository.list_all()

        # 3. Сортировка
        is_descending = order == "desc"
        if sort_by == "priority":
            all_tickets.sort(
                key=lambda item: PRIORITY_WEIGHTS[item.priority],
                reverse=is_descending,
            )
        else:
            all_tickets.sort(key=lambda item: item.created_at, reverse=is_descending)

        # 4. Пагинация: вырезаем нужную страницу 
        total = len(all_tickets)
        start_idx = (page - 1) * effective_page_size
        end_idx = start_idx + effective_page_size
        paginated_items = all_tickets[start_idx:end_idx]

        return TicketPage(
            items=paginated_items,
            total=total,
            page=page,
            page_size=effective_page_size,
        )
        

    def update_ticket(self, ticket_id: int, changes: TicketUpdate) -> Ticket:
        existing_ticket = self._repository.get(ticket_id)
        
        update_data = changes.model_dump(exclude_unset=True)
        
        if "status" in update_data:
            target_status = update_data["status"]
            allowed = ALLOWED_TRANSITIONS.get(existing_ticket.status, set())
            if target_status not in allowed:
                raise InvalidStatusTransitionError(
                    current=existing_ticket.status,
                    target=target_status
                )
            
        updated_ticket = existing_ticket.model_copy(update=update_data)
        return self._repository.update(updated_ticket)

    def delete_ticket(self, ticket_id: int) -> None:
        self._repository.delete(ticket_id)


# --- Зависимости --------------------------------------------------------------

_repository = TicketRepository()


def get_repository() -> TicketRepository:
    return _repository


def get_service(
    repository: TicketRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> TicketService:
    return TicketService(repository, settings)


# --- Приложение ---------------------------------------------------------------

app = FastAPI(title="HelpDesk Lite")


@app.exception_handler(TicketNotFoundError)
def handle_not_found(request: Request, exc: TicketNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(InvalidStatusTransitionError)
def handle_invalid_transition(request: Request, exc: InvalidStatusTransitionError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.post(
    "/tickets",
    response_model=Ticket,
    status_code=status.HTTP_201_CREATED,
    tags=["tickets"],
    summary="Создать тикет",
)
def create_ticket(payload: TicketCreate, service: TicketService = Depends(get_service)):
    return service.create_ticket(payload)


@app.get(
    "/tickets",
    response_model=TicketPage,
    tags=["tickets"],
    summary="Получить список тикетов",
)
def list_tickets(
    sort_by: str = Query("created_at", pattern="^(created_at|priority)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
    service: TicketService = Depends(get_service),
):
    return service.list_tickets(
        sort_by=sort_by,
        order=order,
        page=page,
        page_size=page_size,
    )


@app.get(
    "/tickets/{ticket_id}",
    response_model=Ticket,
    tags=["tickets"],
    summary="Получить тикет по id",
)
def get_ticket(ticket_id: int, service: TicketService = Depends(get_service)):
    return service.get_ticket(ticket_id)


@app.patch(
    "/tickets/{ticket_id}",
    response_model=Ticket,
    tags=["tickets"],
    summary="Изменить часть данных тикета",
)
def update_ticket(
    ticket_id: int,
    changes: TicketUpdate,
    service: TicketService = Depends(get_service),
):
    return service.update_ticket(ticket_id, changes)


@app.delete(
    "/tickets/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tickets"],
    summary="Удалить тикет",
)
def delete_ticket(ticket_id: int, service: TicketService = Depends(get_service)):
    service.delete_ticket(ticket_id)
