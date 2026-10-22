"""A Local To-do todo platform."""

import asyncio
import datetime
import logging
from typing import override

from ical.calendar import Calendar
from ical.calendar_stream import IcsCalendarStream
from ical.store import TodoStore
from ical.todo import Todo, TodoStatus
from ical.types import RelatedTo

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.setup import SetupPhases, async_pause_setup
from homeassistant.util import dt as dt_util

from . import LocalTodoConfigEntry
from .const import CONF_TODO_LIST_NAME
from .store import LocalTodoListStore

_LOGGER = logging.getLogger(__name__)


PRODID = "-//homeassistant.io//local_todo 2.0//EN"
PRODID_REQUIRES_MIGRATION = "-//homeassistant.io//local_todo 1.0//EN"

ICS_TODO_STATUS_MAP = {
    TodoStatus.IN_PROCESS: TodoItemStatus.NEEDS_ACTION,
    TodoStatus.NEEDS_ACTION: TodoItemStatus.NEEDS_ACTION,
    TodoStatus.COMPLETED: TodoItemStatus.COMPLETED,
    TodoStatus.CANCELLED: TodoItemStatus.COMPLETED,
}
ICS_TODO_STATUS_MAP_INV = {
    TodoItemStatus.COMPLETED: TodoStatus.COMPLETED,
    TodoItemStatus.NEEDS_ACTION: TodoStatus.NEEDS_ACTION,
}


def _migrate_calendar(calendar: Calendar) -> bool:
    """Upgrade due dates to rfc5545 format.

    In rfc5545 due dates are exclusive, however we previously set the due date
    as inclusive based on what the user set in the UI. A task is considered
    overdue at midnight at the start of a date so we need to shift the due date
    to the next day for old calendar versions.
    """
    if calendar.prodid is None or calendar.prodid != PRODID_REQUIRES_MIGRATION:
        return False
    migrated = False
    for todo in calendar.todos:
        if todo.due is None or isinstance(todo.due, datetime.datetime):
            continue
        todo.due += datetime.timedelta(days=1)
        migrated = True
    return migrated


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LocalTodoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the local_todo todo platform."""

    store = config_entry.runtime_data
    ics = await store.async_load()

    with async_pause_setup(hass, SetupPhases.WAIT_IMPORT_PACKAGES):
        # calendar_from_ics will dynamically load packages
        # the first time it is called, so we need to do it
        # in a separate thread to avoid blocking the event loop
        calendar: Calendar = await hass.async_add_import_executor_job(
            IcsCalendarStream.calendar_from_ics, ics
        )
    migrated = _migrate_calendar(calendar)
    calendar.prodid = PRODID

    name = config_entry.data[CONF_TODO_LIST_NAME]
    entity = LocalTodoListEntity(store, calendar, name, unique_id=config_entry.entry_id)
    async_add_entities([entity], True)

    if migrated:
        await entity.async_save()


def _convert_item(item: TodoItem) -> Todo:
    """Convert a HomeAssistant TodoItem to an ical Todo."""
    todo = Todo()
    if item.uid:
        todo.uid = item.uid
    if item.summary:
        todo.summary = item.summary
    if item.status:
        todo.status = ICS_TODO_STATUS_MAP_INV[item.status]
    todo.due = item.due
    if todo.due and not isinstance(todo.due, datetime.datetime):
        todo.due += datetime.timedelta(days=1)
    todo.description = item.description
    if item.parent_uid:
        todo.related_to = [RelatedTo(uid=item.parent_uid)]
    return todo


def _due_to_datetime(due: datetime.date | datetime.datetime) -> datetime.datetime:
    """Normalize a due value so dates and datetimes can be compared."""
    tz = dt_util.get_default_time_zone()
    if isinstance(due, datetime.datetime):
        return due if due.tzinfo else due.replace(tzinfo=tz)
    return datetime.datetime.combine(due, datetime.time.min, tzinfo=tz)


class LocalTodoListEntity(TodoListEntity):
    """A To-do List representation of the Shopping List."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.MOVE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    _attr_should_poll = False

    def __init__(
        self,
        store: LocalTodoListStore,
        calendar: Calendar,
        name: str,
        unique_id: str,
    ) -> None:
        """Initialize LocalTodoListEntity."""
        self._store = store
        self._calendar = calendar
        self._calendar_lock = asyncio.Lock()
        self._attr_name = name
        self._attr_unique_id = unique_id

    def _new_todo_store(self) -> TodoStore:
        return TodoStore(self._calendar, tzinfo=dt_util.get_default_time_zone())

    async def async_update(self) -> None:
        """Update entity state based on the local To-do items."""
        todo_items = []
        for item in self._calendar.todos:
            if (due := item.due) and not isinstance(due, datetime.datetime):
                due -= datetime.timedelta(days=1)
            todo_items.append(
                TodoItem(
                    uid=item.uid,
                    summary=item.summary or "",
                    status=ICS_TODO_STATUS_MAP.get(
                        item.status or TodoStatus.NEEDS_ACTION,
                        TodoItemStatus.NEEDS_ACTION,
                    ),
                    due=due,
                    description=item.description,
                    completed=item.completed,
                    parent_uid=item.related_to[0].uid
                    if len(item.related_to) > 0
                    else None,
                )
            )
        self._attr_todo_items = todo_items

    def _validate_due_rules(self, todo: Todo) -> None:
        todos = self._calendar.todos
        existing = next((t for t in todos if t.uid == todo.uid), None)
        if todo.due is None or (existing and existing.due == todo.due):
            return

        new_due = _due_to_datetime(todo.due)

        # rule for a child cannot be due after its parent
        parent_uid = todo.related_to[0].uid if todo.related_to else None
        if parent_uid and parent_uid != todo.uid:
            parent = next((t for t in todos if t.uid == parent_uid), None)
            if (
                parent is not None
                and parent.due is not None
                and new_due > _due_to_datetime(parent.due)
            ):
                raise ServiceValidationError(
                    "A subtask cannot be due after its parent task"
                )

        # rule for a parent cannot be due before any of its children
        for child in todos:
            if (
                child.uid != todo.uid
                and child.related_to
                and child.related_to[0].uid == todo.uid
                and child.due is not None
                and _due_to_datetime(child.due) > new_due
            ):
                raise ServiceValidationError(
                    "A parent task cannot be due before its subtasks"
                )

    def _descendant_uids(self, uid: str) -> list[str]:
        """Return the uids of all subtasks (children, grandchildren, ...) of a task."""
        todos = self._calendar.todos
        result: list[str] = []
        seen = {uid}
        queue = [uid]
        while queue:
            current = queue.pop()
            for t in todos:
                if (
                    t.related_to
                    and t.related_to[0].uid == current
                    and t.uid not in seen
                ):
                    seen.add(t.uid)
                    result.append(t.uid)
                    queue.append(t.uid)
        return result

    def _ancestor_uids(self, uid: str) -> list[str]:
        """Return the uids of a task's parent, grandparent, ... up to the top."""
        by_uid = {t.uid: t for t in self._calendar.todos}
        result: list[str] = []
        seen = {uid}
        current = by_uid.get(uid)
        while current and current.related_to:
            parent_uid = current.related_to[0].uid
            if parent_uid in seen:
                break
            seen.add(parent_uid)
            result.append(parent_uid)
            current = by_uid.get(parent_uid)
        return result

    def _update_related_todo_statuses(
        self, item: TodoItem, uid: str, was_completed: bool
    ) -> None:
        """Propagate status changes to related todo items."""
        if item.status == TodoItemStatus.COMPLETED and not was_completed:
            descendants = set(self._descendant_uids(uid))
            for todo in self._calendar.todos:
                if todo.uid in descendants:
                    todo.status = TodoStatus.COMPLETED
                    todo.completed = dt_util.utcnow()
        elif item.status == TodoItemStatus.NEEDS_ACTION and was_completed:
            ancestors = set(self._ancestor_uids(uid))
            for todo in self._calendar.todos:
                if todo.uid in ancestors:
                    todo.status = TodoStatus.NEEDS_ACTION
                    todo.completed = None

    @override
    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add an item to the To-do list."""
        todo = _convert_item(item)
        async with self._calendar_lock:
            todo_store = self._new_todo_store()
            await self.hass.async_add_executor_job(todo_store.add, todo)
            await self.async_save()
        await self.async_update_ha_state(force_refresh=True)

    @override
    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Update an item in the To-do list."""
        todo = _convert_item(item)
        async with self._calendar_lock:
            self._validate_due_rules(todo)
            previous = next(
                (t for t in self._calendar.todos if t.uid == todo.uid), None
            )
            was_completed = (
                previous is not None
                and ICS_TODO_STATUS_MAP.get(previous.status or TodoStatus.NEEDS_ACTION)
                == TodoItemStatus.COMPLETED
            )
            todo_store = self._new_todo_store()
            original = list(self._calendar.todos)
            await self.hass.async_add_executor_job(todo_store.edit, todo.uid, todo)
            # ical's edit() deletes an item's children as a side effect, so restore the original list, using the freshly edited object for this item
            edited = next(t for t in self._calendar.todos if t.uid == todo.uid)
            self._calendar.todos[:] = [
                edited if t.uid == todo.uid else t for t in original
            ]
            self._update_related_todo_statuses(item, todo.uid, was_completed)
            await self.async_save()
        await self.async_update_ha_state(force_refresh=True)

    @override
    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Delete an item from the To-do list."""
        async with self._calendar_lock:
            todo_store = self._new_todo_store()
            for uid in uids:
                todo_store.delete(uid)
            await self.async_save()
        await self.async_update_ha_state(force_refresh=True)

    @override
    async def async_move_todo_item(
        self, uid: str, previous_uid: str | None = None, parent_uid: str | None = None
    ) -> None:
        """Re-order an item to the To-do list."""
        async with self._calendar_lock:
            todos = self._calendar.todos
            item_idx: dict[str, int] = {itm.uid: idx for idx, itm in enumerate(todos)}
            if uid not in item_idx:
                raise HomeAssistantError(
                    f"Item '{uid}' not found in todo list {self.entity_id}"
                )
            if previous_uid and previous_uid not in item_idx:
                raise HomeAssistantError(
                    f"Item '{previous_uid}' not found in todo list {self.entity_id}"
                )
            if parent_uid and parent_uid not in item_idx:
                raise HomeAssistantError(
                    f"Item '{parent_uid}' not found in todo list {self.entity_id}"
                )
            dst_idx = item_idx[previous_uid] + 1 if previous_uid else 0
            src_idx = item_idx[uid]
            parent_item = todos[item_idx[parent_uid]] if parent_uid else None
            src_item = todos.pop(src_idx)

            if parent_uid:
                if (
                    parent_item is not None
                    and src_item.due is not None
                    and parent_item.due is not None
                    and _due_to_datetime(src_item.due)
                    > _due_to_datetime(parent_item.due)
                ):
                    src_item.due = None
                src_item.related_to = [RelatedTo(uid=parent_uid)]
            else:
                src_item.related_to = []

            if dst_idx > src_idx:
                dst_idx -= 1
            todos.insert(dst_idx, src_item)
            await self.async_save()
        await self.async_update_ha_state(force_refresh=True)

    async def async_save(self) -> None:
        """Persist the todo list to disk."""
        content = IcsCalendarStream.calendar_to_ics(self._calendar)
        await self._store.async_store(content)
