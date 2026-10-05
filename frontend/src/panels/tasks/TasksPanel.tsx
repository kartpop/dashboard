import {
  DndContext,
  type DragEndEvent,
  type CollisionDetection,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  pointerWithin,
  useDroppable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import {
  type Bucket,
  type BucketItem,
  type Group,
  type Task,
  type TaskList,
} from "./useTasksPanel";
import {
  type ListRef,
  type TaskActions,
  type TasksHook,
  type ThreadRef,
  allListRefs,
  bucketHeading,
  bucketUrgencyClass,
  buildTaskActions,
  dueToDateInput,
} from "./taskRows";

export function ThreadBadgeIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      aria-hidden
    >
      <circle cx="2" cy="6" r="1.5" fill="currentColor" />
      <circle cx="6.5" cy="6" r="1.5" fill="currentColor" />
      <circle cx="10.5" cy="6" r="1.3" />
      <path d="M3.5 6h1.5M8 6h1.2" />
    </svg>
  );
}

// ── Rank helpers ──────────────────────────────────────────────────────────────

function effectiveRank(item: BucketItem, index: number): number {
  return item.rank ?? (index + 1) * 1000;
}

function computeMidpointRank(items: BucketItem[], toIndex: number): number {
  const prev = toIndex > 0 ? items[toIndex - 1] : null;
  const next = toIndex < items.length - 1 ? items[toIndex + 1] : null;
  const prevRank = prev
    ? effectiveRank(prev, toIndex - 1)
    : next
      ? effectiveRank(next, toIndex + 1) - 2000
      : 1000;
  const nextRank = next ? effectiveRank(next, toIndex + 1) : prevRank + 2000;
  return (prevRank + nextRank) / 2;
}

function computeGroupTaskRank(tasks: Task[], toIndex: number): number {
  const prevRank =
    toIndex > 0
      ? (tasks[toIndex - 1].rank ?? toIndex * 1000)
      : toIndex < tasks.length - 1
        ? (tasks[toIndex + 1].rank ?? (toIndex + 1) * 1000) - 2000
        : 1000;
  const nextRank =
    toIndex < tasks.length - 1
      ? (tasks[toIndex + 1].rank ?? (toIndex + 1) * 1000)
      : prevRank + 2000;
  return (prevRank + nextRank) / 2;
}

// ── Data model helpers ────────────────────────────────────────────────────────

/** Flat list of IDs for the SortableContext (group header + its task IDs in order). */
function flatItemIds(items: BucketItem[]): string[] {
  const ids: string[] = [];
  for (const item of items) {
    if (item.type === "task") {
      ids.push(item.id);
    } else {
      ids.push(`group-${item.id}`);
      for (const t of item.items) ids.push(t.id);
    }
  }
  return ids;
}

type ContainerRef = { type: "bucket" } | { type: "group"; groupId: number };

function findContainer(id: string, items: BucketItem[]): ContainerRef | null {
  for (const item of items) {
    if (item.type === "task" && item.id === id) return { type: "bucket" };
    if (item.type === "group") {
      for (const t of item.items) {
        if (t.id === id) return { type: "group", groupId: item.id };
      }
    }
  }
  return null;
}

function findBucketItemIndex(id: string, items: BucketItem[]): number {
  return items.findIndex(
    (it) =>
      (it.type === "task" && it.id === id) ||
      (it.type === "group" && `group-${it.id}` === id),
  );
}

/**
 * Find which bucket of a list contains a given sortable id. `id` may be a
 * standalone task id, a `group-{id}` header, or a task id nested in a group.
 */
function findBucketForId(buckets: Bucket[], id: string): Bucket | null {
  if (id.startsWith("group-")) {
    const groupId = parseInt(id.slice(6), 10);
    return (
      buckets.find((b) =>
        b.items.some((it) => it.type === "group" && it.id === groupId),
      ) ?? null
    );
  }
  return (
    buckets.find(
      (b) =>
        b.items.some((it) => it.type === "task" && it.id === id) ||
        b.items.some(
          (it) => it.type === "group" && it.items.some((t) => t.id === id),
        ),
    ) ?? null
  );
}

// ── Draggable task ────────────────────────────────────────────────────────────

interface SortableTaskProps {
  task: Task;
  compact?: boolean;
  actions: TaskActions;
}

function SortableTask({ task, compact, actions }: SortableTaskProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
    id: task.id,
    data: { type: "task" },
  });

  const [menuOpen, setMenuOpen] = useState(false);
  // Menu popover is rendered in a portal (escapes the group's clip context, so
  // it is never truncated for a task low in a tall group or a one-item group).
  const [menuPos, setMenuPos] = useState<{ top: number; right: number } | null>(
    null,
  );
  const menuBtnRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);

  const [editing, setEditing] = useState(false);
  const [titleInput, setTitleInput] = useState(task.title);
  const [notesOpen, setNotesOpen] = useState(false);
  const [notesInput, setNotesInput] = useState(task.notes ?? "");

  // The task changed underneath us (a refresh after a thread-popover edit, the
  // phone, …): pull the new title/notes into the local drafts — unless the user
  // has unsaved typing in them (draft ≠ the value it was seeded from).
  const [seen, setSeen] = useState({ title: task.title, notes: task.notes });
  if (task.title !== seen.title || task.notes !== seen.notes) {
    setSeen({ title: task.title, notes: task.notes });
    if (!editing || titleInput === seen.title) setTitleInput(task.title);
    if (notesInput === (seen.notes ?? "")) setNotesInput(task.notes ?? "");
  }

  useEffect(() => {
    if (!menuOpen) return;
    function onPointerDown(e: PointerEvent) {
      const t = e.target as Node;
      if (
        !menuBtnRef.current?.contains(t) &&
        !popoverRef.current?.contains(t)
      ) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [menuOpen]);

  function openMenu() {
    const rect = menuBtnRef.current?.getBoundingClientRect();
    if (rect) {
      setMenuPos({
        top: rect.bottom + 2,
        right: window.innerWidth - rect.right,
      });
    }
    setMenuOpen((o) => !o);
  }

  function commitTitle() {
    setEditing(false);
    const trimmed = titleInput.trim();
    if (trimmed && trimmed !== task.title) {
      actions.onEditTitle(task.id, trimmed); // same-value fires no PATCH
    } else {
      setTitleInput(task.title);
    }
  }

  function commitNotes() {
    const next = notesInput;
    if (next !== (task.notes ?? "")) {
      actions.onEditNotes(task.id, next);
    }
  }

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  };

  const thread = actions.threadFor?.(task.id);

  return (
    <li
      ref={setNodeRef}
      style={style}
      className={`task-item${compact ? " task-item--compact" : ""}`}
    >
      <div className="task-row">
        <span
          className="drag-handle"
          {...attributes}
          {...listeners}
          aria-label="drag to reorder"
        >
          ⠿
        </span>
        <input
          type="checkbox"
          className="task-check"
          checked={task.status === "completed"}
          aria-label="complete task"
          onPointerDown={(e) => e.stopPropagation()}
          onChange={() => actions.onComplete(task.id)}
        />
        {editing ? (
          <input
            className="task-title-input"
            value={titleInput}
            autoFocus
            onChange={(e) => setTitleInput(e.target.value)}
            onBlur={commitTitle}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitTitle();
              if (e.key === "Escape") {
                setEditing(false);
                setTitleInput(task.title);
              }
            }}
          />
        ) : (
          <span
            className="task-title"
            title={task.title}
            onClick={() => {
              setTitleInput(task.title);
              setEditing(true);
            }}
          >
            {task.title}
          </span>
        )}
        {thread && (
          // Its own control, like every other non-handle control: it stops
          // pointerdown so clicking it never starts a drag (tasks-panel.md).
          <button
            className="task-thread-badge"
            title={`Thread: ${thread.title}`}
            aria-label={`Open thread ${thread.title}`}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => actions.onOpenThread?.(thread.id)}
          >
            <ThreadBadgeIcon />
            <span>{thread.title}</span>
          </button>
        )}
        <button
          className={`notes-toggle${notesOpen ? " notes-toggle--open" : ""}${task.notes ? " notes-toggle--has" : ""}`}
          aria-label={notesOpen ? "hide notes" : "show notes"}
          aria-expanded={notesOpen}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => {
            setNotesInput(task.notes ?? "");
            setNotesOpen((o) => !o);
          }}
        >
          ▸
        </button>
        <input
          type="date"
          className="task-date"
          value={dueToDateInput(task.due)}
          aria-label="due date"
          onPointerDown={(e) => e.stopPropagation()}
          onChange={(e) =>
            actions.onSetDueDate(task.id, e.target.value || null)
          }
        />
        <button
          ref={menuBtnRef}
          className="task-menu"
          aria-label="task actions"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            openMenu();
          }}
        >
          ⋯
        </button>
      </div>
      {notesOpen && (
        <textarea
          className="task-notes"
          placeholder="Add notes"
          value={notesInput}
          onPointerDown={(e) => e.stopPropagation()}
          onChange={(e) => setNotesInput(e.target.value)}
          onBlur={commitNotes}
        />
      )}
      {menuOpen &&
        menuPos &&
        createPortal(
          <div
            ref={popoverRef}
            className="task-menu-popover"
            role="menu"
            style={{ top: menuPos.top, right: menuPos.right }}
          >
            <span className="task-menu-title">Move to list…</span>
            {actions.otherLists.length === 0 ? (
              <span className="task-menu-empty">No other lists</span>
            ) : (
              actions.otherLists.map((l) => (
                <button
                  key={l.id}
                  className="move-to-list-option"
                  role="menuitem"
                  onClick={() => {
                    setMenuOpen(false);
                    actions.onMoveToList(task.id, l.id);
                  }}
                >
                  {l.title}
                </button>
              ))
            )}
            <button
              className="task-menu-delete"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                actions.onDelete(task.id);
              }}
            >
              Delete
            </button>
          </div>,
          document.body,
        )}
    </li>
  );
}

// ── Group container ───────────────────────────────────────────────────────────

interface GroupContainerProps {
  group: Group;
  actions: TaskActions;
  onRename: (groupId: number, name: string) => void;
  onDelete: (groupId: number) => void;
}

function GroupContainer({
  group,
  actions,
  onRename,
  onDelete,
}: GroupContainerProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
    id: `group-${group.id}`,
    data: { type: "group" },
  });

  const [editing, setEditing] = useState(false);
  const [nameInput, setNameInput] = useState(group.name);

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  };

  function commitRename() {
    setEditing(false);
    const trimmed = nameInput.trim();
    if (trimmed && trimmed !== group.name) {
      onRename(group.id, trimmed);
    } else {
      setNameInput(group.name);
    }
  }

  return (
    <div ref={setNodeRef} style={style} className="group-container">
      <div className="group-header">
        <span
          className="drag-handle group-drag-handle"
          {...attributes}
          {...listeners}
          aria-label="drag group to reorder"
        >
          ⠿
        </span>
        {editing ? (
          <input
            className="group-name-input"
            value={nameInput}
            autoFocus
            onChange={(e) => setNameInput(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitRename();
              if (e.key === "Escape") {
                setEditing(false);
                setNameInput(group.name);
              }
            }}
          />
        ) : (
          <button
            className="group-name"
            onClick={() => setEditing(true)}
            title="Click to rename"
          >
            {group.name}
          </button>
        )}
        <button
          className="group-delete"
          onClick={() => onDelete(group.id)}
          title="Delete group (tasks become standalone)"
          aria-label="delete group"
        >
          ×
        </button>
      </div>
      <ul className="group-tasks">
        {group.items.map((task) => (
          <SortableTask key={task.id} task={task} compact actions={actions} />
        ))}
      </ul>
    </div>
  );
}

// ── Collision detection ───────────────────────────────────────────────────────
// pointerWithin first: when the pointer is physically inside a sortable element's
// rect we use that hit (critical for drag-into-group). Fall back to closestCenter
// for inter-item gaps where nothing contains the pointer.
const collisionDetection: CollisionDetection = (args) => {
  const hits = pointerWithin(args);
  return hits.length > 0 ? hits : closestCenter(args);
};

// ── Bucket section ────────────────────────────────────────────────────────────

interface BucketSectionProps {
  bucket: Bucket;
  list: TaskList;
  actions: TaskActions;
  onRenameGroup: (
    tasklistId: string,
    groupId: number,
    bucketKey: string,
    name: string,
  ) => void;
  onDeleteGroup: (
    tasklistId: string,
    groupId: number,
    bucketKey: string,
  ) => void;
  onCreateGroup: (tasklistId: string, bucketKey: string, name: string) => void;
}

function BucketSection({
  bucket,
  list,
  actions,
  onRenameGroup,
  onDeleteGroup,
  onCreateGroup,
}: BucketSectionProps) {
  const [addingGroup, setAddingGroup] = useState(false);
  const [newGroupName, setNewGroupName] = useState("");

  // Droppable for the whole bucket so empty/open-area drops resolve to a bucket.
  // The id is list-scoped (goal 6: one shared DndContext spans the pinned pair,
  // so `bucket:{key}` alone would collide across the two lists' same-named
  // buckets). `data` carries the resolved list + bucket for the drag handler.
  const { setNodeRef: setDroppableRef } = useDroppable({
    id: `bucket:${list.id}:${bucket.key}`,
    data: { listId: list.id, bucketKey: bucket.key },
  });

  const ids = flatItemIds(bucket.items);

  function submitNewGroup() {
    const name = newGroupName.trim();
    if (!name) return;
    onCreateGroup(list.id, bucket.key, name);
    setNewGroupName("");
    setAddingGroup(false);
  }

  const urgency = bucketUrgencyClass(bucket);

  return (
    <div className={`date-group${urgency ? ` ${urgency}` : ""}`}>
      <span className="date-group-label">{bucketHeading(bucket)}</span>
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        <ul ref={setDroppableRef} className="bucket-droppable">
          {bucket.items.map((item) =>
            item.type === "task" ? (
              <SortableTask key={item.id} task={item} actions={actions} />
            ) : (
              <li key={`group-${item.id}`} className="group-item-wrapper">
                <GroupContainer
                  group={item}
                  actions={actions}
                  onRename={(gid, name) =>
                    onRenameGroup(list.id, gid, bucket.key, name)
                  }
                  onDelete={(gid) => onDeleteGroup(list.id, gid, bucket.key)}
                />
              </li>
            ),
          )}
        </ul>
      </SortableContext>
      {addingGroup ? (
        <div className="add-group-form">
          <input
            className="group-name-input"
            placeholder="Group name"
            value={newGroupName}
            autoFocus
            onChange={(e) => setNewGroupName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submitNewGroup();
              if (e.key === "Escape") {
                setAddingGroup(false);
                setNewGroupName("");
              }
            }}
          />
          <button className="add-group-confirm" onClick={submitNewGroup}>
            Add
          </button>
          <button
            className="add-group-cancel"
            onClick={() => {
              setAddingGroup(false);
              setNewGroupName("");
            }}
          >
            Cancel
          </button>
        </div>
      ) : (
        <button className="add-group-btn" onClick={() => setAddingGroup(true)}>
          + group
        </button>
      )}
    </div>
  );
}

// ── Task list column (presentational, no DndContext) ──────────────────────────
// Renders one list's header + add-task + buckets. The enclosing DndListGroup
// owns the shared DndContext + drag handler, so a column never wraps its own
// context (goal 6: the pinned pair shares ONE context to enable cross-list drag).

function TaskListColumn({
  list,
  allLists,
  tasks,
  compactDates,
  className,
  threadLinks,
  onOpenThread,
}: {
  list: TaskList;
  allLists: ListRef[];
  tasks: TasksHook;
  // Pinned columns (My Tasks / Follow-ups) hide the per-row date — the bucket is
  // already the date — keeping only the calendar-picker icon (see CSS).
  compactDates?: boolean;
  className?: string;
  threadLinks?: Map<string, ThreadRef>;
  onOpenThread?: (threadId: number) => void;
}) {
  const actions = buildTaskActions(
    list,
    allLists,
    tasks,
    threadLinks,
    onOpenThread,
  );

  // Inline rename of the list header + the per-list "+ add task" affordance.
  const [renaming, setRenaming] = useState(false);
  const [titleInput, setTitleInput] = useState(list.title);
  const [adding, setAdding] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newNotes, setNewNotes] = useState("");
  const [newDueDate, setNewDueDate] = useState("");

  function commitListRename() {
    setRenaming(false);
    const trimmed = titleInput.trim();
    if (trimmed && trimmed !== list.title) {
      tasks.renameList(list.id, trimmed); // same-value fires no PATCH
    } else {
      setTitleInput(list.title);
    }
  }

  function resetAddForm() {
    setNewTitle("");
    setNewNotes("");
    setNewDueDate("");
    setAdding(false);
  }

  function submitNewTask() {
    const title = newTitle.trim();
    if (!title) return;
    void tasks.createTask(list.id, title, {
      notes: newNotes.trim() || null,
      dueDate: newDueDate || null,
    });
    resetAddForm();
  }

  return (
    <section
      className={`panel task-column${compactDates ? " task-column--compact-dates" : ""}${className ? ` ${className}` : ""}`}
    >
      <div className="panel-head">
        {renaming ? (
          <input
            className="list-title-input"
            value={titleInput}
            autoFocus
            onChange={(e) => setTitleInput(e.target.value)}
            onBlur={commitListRename}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitListRename();
              if (e.key === "Escape") {
                setRenaming(false);
                setTitleInput(list.title);
              }
            }}
          />
        ) : (
          <h2
            className="list-title"
            title="Click to rename list"
            onClick={() => {
              setTitleInput(list.title);
              setRenaming(true);
            }}
          >
            {list.title}
          </h2>
        )}
        <button
          className="panel-refresh"
          aria-label="refresh tasks"
          title="Refresh"
          onClick={tasks.refresh}
        >
          ⟳
        </button>
      </div>
      {/* Everything below the header scrolls internally (goal 7a height cap); the
          header (title + refresh) stays pinned while the list scrolls. */}
      <div className="task-column-body">
        {adding ? (
          <div className="add-task-form">
            <div className="add-task-top-row">
              <input
                className="add-task-input"
                placeholder="New task"
                value={newTitle}
                autoFocus
                onChange={(e) => setNewTitle(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submitNewTask();
                  if (e.key === "Escape") resetAddForm();
                }}
              />
              <input
                type="date"
                className="task-date"
                value={newDueDate}
                aria-label="due date"
                onChange={(e) => setNewDueDate(e.target.value)}
              />
              <button className="add-task-confirm" onClick={submitNewTask}>
                Add
              </button>
              <button className="add-task-cancel" onClick={resetAddForm}>
                Cancel
              </button>
            </div>
            <textarea
              className="add-task-notes-input"
              placeholder="Add notes"
              value={newNotes}
              onChange={(e) => setNewNotes(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") resetAddForm();
              }}
            />
          </div>
        ) : (
          <button className="add-task-btn" onClick={() => setAdding(true)}>
            + add task
          </button>
        )}
        {list.buckets.map((bucket) => (
          <BucketSection
            key={bucket.key}
            bucket={bucket}
            list={list}
            actions={actions}
            onRenameGroup={tasks.renameGroup}
            onDeleteGroup={tasks.deleteGroup}
            onCreateGroup={(listId, bucketKey, name) =>
              void tasks.createGroup(listId, bucketKey, name)
            }
          />
        ))}
      </div>
    </section>
  );
}

// ── DnD list group (shared DndContext over one OR two lists) ───────────────────
// g4 used one DndContext per list; g6 lets a single context span the pinned pair
// so a task can be dragged BETWEEN lists. The handler resolves source/dest list +
// bucket across all lists under the context, then dispatches:
//   • same list, same bucket  → overlay reorder / move-into-group (g3, unchanged)
//   • same list, other bucket → reschedule (g4, unchanged)
//   • different list          → cross-list move (g6, may also reschedule + group)
// A single list under the context (the "Other tasks" lists) never hits the
// cross-list branch, so their behavior is identical to g4.

function findListAndBucket(
  lists: TaskList[],
  id: string,
): { list: TaskList; bucket: Bucket } | null {
  for (const list of lists) {
    const bucket = findBucketForId(list.buckets, id);
    if (bucket) return { list, bucket };
  }
  return null;
}

function findTaskInBucket(bucket: Bucket, taskId: string): Task | null {
  for (const it of bucket.items) {
    if (it.type === "task" && it.id === taskId) return it;
    if (it.type === "group") {
      const f = it.items.find((t) => t.id === taskId);
      if (f) return f;
    }
  }
  return null;
}

function DndListGroup({
  lists,
  tasks,
  children,
}: {
  // The lists under this context, used by handleDragEnd to resolve src/dest.
  // Rendering the columns is the CALLER's job (passed as children) so a resize
  // handle can be interleaved between the two pinned columns as a grid sibling.
  lists: TaskList[];
  tasks: TasksHook;
  children: ReactNode;
}) {
  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  // Keep a ref to the latest lists for handleDragEnd (a closure invoked after
  // render). Update in an effect so we never write to a ref during render.
  const listsRef = useRef(lists);
  useEffect(() => {
    listsRef.current = lists;
  }, [lists]);

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    const lists = listsRef.current;
    const activeId = String(active.id);
    const overId = String(over.id);

    // Resolve source list + bucket (which holds activeId).
    const src = findListAndBucket(lists, activeId);
    if (!src) return;
    const srcList = src.list;
    const srcBucket = src.bucket;

    // Resolve destination list + bucket. overId is a task id, a `group-{id}`, a
    // task inside a group, or a `bucket:{listId}:{key}` droppable (its resolved
    // list/bucket ride `over.data.current`).
    let destList: TaskList;
    let destBucket: Bucket;
    const overData = over.data?.current as
      | { listId?: string; bucketKey?: string }
      | undefined;
    if (overId.startsWith("bucket:") && overData?.listId != null) {
      const dl = lists.find((l) => l.id === overData.listId);
      const db = dl?.buckets.find((b) => b.key === overData.bucketKey);
      if (!dl || !db) return;
      destList = dl;
      destBucket = db;
    } else {
      const dest = findListAndBucket(lists, overId);
      if (!dest) return;
      destList = dest.list;
      destBucket = dest.bucket;
    }

    const crossList = destList.id !== srcList.id;

    // ── Group reorder (groups never span buckets OR lists) ────────────────────
    if (activeId.startsWith("group-")) {
      if (crossList) return;
      if (destBucket.key !== srcBucket.key) return;
      const items = srcBucket.items;
      const groupId = parseInt(activeId.slice(6), 10);
      const fromIndex = findBucketItemIndex(activeId, items);
      if (fromIndex === -1) return;

      let toIndex: number;
      if (overId.startsWith("bucket:")) {
        // dropping on the open bucket area → move to the end
        toIndex = items.length - 1;
      } else {
        // overId may be a task inside a group (flat SortableContext) — resolve
        // it to that group's header id before indexing at bucket level.
        let resolvedOverId = overId;
        if (!overId.startsWith("group-")) {
          const overContainer = findContainer(overId, items);
          if (overContainer?.type === "group")
            resolvedOverId = `group-${overContainer.groupId}`;
        }
        toIndex = findBucketItemIndex(resolvedOverId, items);
      }
      if (toIndex === -1 || fromIndex === toIndex) return;

      const reordered = [...items];
      const [moved] = reordered.splice(fromIndex, 1);
      reordered.splice(toIndex, 0, moved);
      const newRank = computeMidpointRank(reordered, toIndex);
      tasks.reorderGroup(
        srcList.id,
        groupId,
        srcBucket.key,
        fromIndex,
        toIndex,
        newRank,
      );
      return;
    }

    // ── Task drag ─────────────────────────────────────────────────────────────
    // Resolve the destination container (bucket open-area, a group, or a
    // specific task position) within destBucket.
    const destItems = destBucket.items;
    let destContainer: ContainerRef;
    let destIndexInContainer: number;

    if (overId.startsWith("bucket:")) {
      // Dropped on the open bucket area → append to the end of standalone items.
      destContainer = { type: "bucket" };
      destIndexInContainer = destItems.length;
    } else if (overId.startsWith("group-")) {
      // Dropped on a group header → append to that group.
      const groupId = parseInt(overId.slice(6), 10);
      const grp = destItems.find(
        (it): it is Group => it.type === "group" && it.id === groupId,
      );
      if (!grp) return;
      destContainer = { type: "group", groupId };
      destIndexInContainer = grp.items.length;
    } else {
      const overContainer = findContainer(overId, destItems);
      if (!overContainer) return;
      destContainer = overContainer;
      if (overContainer.type === "bucket") {
        destIndexInContainer = destItems.findIndex(
          (it) => it.type === "task" && it.id === overId,
        );
      } else {
        const grp = destItems.find(
          (it): it is Group =>
            it.type === "group" && it.id === overContainer.groupId,
        );
        if (!grp) return;
        destIndexInContainer = grp.items.findIndex((t) => t.id === overId);
      }
    }

    const destGroupId =
      destContainer.type === "group" ? destContainer.groupId : null;

    // Compute destIndex + newRank against the DEST container, with the dragged
    // task hypothetically inserted at the drop position (shared by the cross-list
    // and reschedule branches).
    function destPlacement(movedTask: Task): {
      destIndex: number;
      newRank: number;
    } {
      if (destGroupId === null) {
        const toIdx = Math.min(destIndexInContainer, destItems.length);
        const clone = [...destItems];
        clone.splice(toIdx, 0, { ...movedTask, rank: null } as Task);
        return { destIndex: toIdx, newRank: computeMidpointRank(clone, toIdx) };
      }
      const destGrp = destItems.find(
        (it): it is Group => it.type === "group" && it.id === destGroupId,
      )!;
      const toIdx = Math.min(destIndexInContainer, destGrp.items.length);
      const clone = [...destGrp.items];
      clone.splice(toIdx, 0, { ...movedTask, rank: null } as Task);
      return { destIndex: toIdx, newRank: computeGroupTaskRank(clone, toIdx) };
    }

    // ── Cross-list move (goal 6) ──────────────────────────────────────────────
    if (crossList) {
      // Overdue is a render-only rollup, not a real date — can't drag INTO it
      // (past dates go through the picker). Dragging a task that IS overdue out
      // to a real bucket is fine; same-bucket (Overdue→Overdue) preserves due.
      if (destBucket.key === "OVERDUE" && srcBucket.key !== "OVERDUE") return;
      const movedTask = findTaskInBucket(srcBucket, activeId);
      if (!movedTask) return;
      if (
        destGroupId !== null &&
        !destItems.some((it) => it.type === "group" && it.id === destGroupId)
      )
        return;

      // Same bucket key → preserve source due (undefined); else set/clear it.
      let dueDate: string | null | undefined;
      if (destBucket.key === srcBucket.key) dueDate = undefined;
      else if (destBucket.key === "NO_DATE") dueDate = null;
      else dueDate = destBucket.key;

      const { destIndex, newRank } = destPlacement(movedTask);
      tasks.moveTaskCrossList(
        srcList.id,
        activeId,
        destList.id,
        destBucket.key,
        dueDate,
        destGroupId,
        destIndex,
        newRank,
      );
      return;
    }

    // ── Same list, cross-bucket drag = RESCHEDULE ─────────────────────────────
    if (destBucket.key !== srcBucket.key) {
      if (destBucket.key === "OVERDUE") return;
      const dueDate = destBucket.key === "NO_DATE" ? null : destBucket.key;
      const movedTask = findTaskInBucket(srcBucket, activeId);
      if (!movedTask) return;
      if (
        destGroupId !== null &&
        !destItems.some((it) => it.type === "group" && it.id === destGroupId)
      )
        return;
      const { destIndex, newRank } = destPlacement(movedTask);
      tasks.rescheduleTask(
        srcList.id,
        activeId,
        srcBucket.key,
        destBucket.key,
        dueDate,
        destGroupId,
        destIndex,
        newRank,
      );
      return;
    }

    // ── Same list, same bucket: behave exactly as goal 3 (overlay PATCH only) ──
    const items = srcBucket.items;
    const srcContainer = findContainer(activeId, items);
    if (!srcContainer) return;

    const sameType =
      srcContainer.type === destContainer.type &&
      (srcContainer.type === "bucket" ||
        (srcContainer as { type: "group"; groupId: number }).groupId ===
          (destContainer as { type: "group"; groupId: number }).groupId);

    if (sameType) {
      // ── Same container: reorder ─────────────────────────────────────────────
      if (srcContainer.type === "bucket") {
        const fromIdx = items.findIndex(
          (it) => it.type === "task" && it.id === activeId,
        );
        if (fromIdx === -1) return;
        const toIdx = destIndexInContainer;
        const reordered = [...items];
        const [moved] = reordered.splice(fromIdx, 1);
        reordered.splice(toIdx, 0, moved);
        const newRank = computeMidpointRank(reordered, toIdx);
        tasks.reorderTask(
          srcList.id,
          activeId,
          srcBucket.key,
          null,
          fromIdx,
          toIdx,
          newRank,
        );
      } else {
        const groupId = (srcContainer as { type: "group"; groupId: number })
          .groupId;
        const grp = items.find(
          (it): it is Group => it.type === "group" && it.id === groupId,
        )!;
        const fromIdx = grp.items.findIndex((t) => t.id === activeId);
        if (fromIdx === -1) return;
        const toIdx = destIndexInContainer;
        const reordered = [...grp.items];
        const [moved] = reordered.splice(fromIdx, 1);
        reordered.splice(toIdx, 0, moved);
        const newRank = computeGroupTaskRank(reordered, toIdx);
        tasks.reorderTask(
          srcList.id,
          activeId,
          srcBucket.key,
          groupId,
          fromIdx,
          toIdx,
          newRank,
        );
      }
    } else {
      // ── Cross-container move within the same bucket ─────────────────────────
      if (destContainer.type === "bucket") {
        // task leaving a group → standalone
        const grp = items.find(
          (it): it is Group =>
            it.type === "group" &&
            it.id ===
              (srcContainer as { type: "group"; groupId: number }).groupId,
        )!;
        const taskBeingMoved = grp.items.find((t) => t.id === activeId)!;

        const bucketLevelItems = items.filter(
          (it) => it.type === "group" || it.id !== activeId,
        ) as BucketItem[];
        const toIdx = Math.min(
          destIndexInContainer,
          bucketLevelItems.length - 1,
        );
        const reordered = [...bucketLevelItems];
        reordered.splice(toIdx, 0, { ...taskBeingMoved, rank: null } as Task);
        const newRank = computeMidpointRank(reordered, toIdx);
        tasks.moveTask(
          srcList.id,
          activeId,
          srcBucket.key,
          null,
          toIdx,
          newRank,
        );
      } else {
        // task moving into a group (from standalone or different group)
        const moveDestGroupId = (
          destContainer as { type: "group"; groupId: number }
        ).groupId;
        const destGrp = items.find(
          (it): it is Group => it.type === "group" && it.id === moveDestGroupId,
        )!;
        const toIdx = Math.min(destIndexInContainer, destGrp.items.length);
        const tasksClone = [...destGrp.items];
        const movedTask = findTaskInBucket(srcBucket, activeId);
        if (!movedTask) return;
        tasksClone.splice(toIdx, 0, { ...movedTask, rank: null } as Task);
        const newRank = computeGroupTaskRank(tasksClone, toIdx);
        tasks.moveTask(
          srcList.id,
          activeId,
          srcBucket.key,
          moveDestGroupId,
          toIdx,
          newRank,
        );
      }
    }
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={collisionDetection}
      onDragEnd={handleDragEnd}
    >
      {children}
    </DndContext>
  );
}

// ── Layout: pinned pair, other tasks, toasts (goal 6) ─────────────────────────

// The two lists that fill the full-width top row, matched by title against the
// live Google lists at load. Static in code (no ui_prefs / visibility chooser
// yet — deferred). A title missing from Google renders an empty-column hint.
export const PINNED_LIST_TITLES = ["My Tasks", "Follow-ups"];

// ── Resizable top row (goal 6a) ───────────────────────────────────────────────
// The three columns (My Tasks | Follow-ups | Scratchpad) live in ONE grid with a
// draggable handle between each adjacent pair, so each column can be squished or
// widened independently. Column widths are `fr` fractions (default 30/30/40) held
// in ephemeral state — no persistence yet (ui_prefs is deferred to goal 9). Below
// a breakpoint the grid stacks and the handles hide (see CSS).

const DEFAULT_WIDTHS = [0.3, 0.3, 0.4];
const HANDLE_PX = 14;
const MIN_FRAC = 0.14;
// Goal 14: the left block's tasks/threads split (tasks share), dragged by the row
// handle between them. Ephemeral like the widths; clamped so neither row vanishes.
const DEFAULT_ROW_SPLIT = 0.45;
const MIN_ROW_FRAC = 0.2;

function ResizeHandle({
  onPointerDown,
  className,
  horizontal,
}: {
  onPointerDown: (e: ReactPointerEvent<HTMLDivElement>) => void;
  className?: string;
  // A row divider (drag up/down) rather than a column divider.
  horizontal?: boolean;
}) {
  return (
    <div
      className={`resize-handle${horizontal ? " resize-handle--row" : ""}${className ? ` ${className}` : ""}`}
      role="separator"
      aria-orientation={horizontal ? "horizontal" : "vertical"}
      aria-label={horizontal ? "resize tasks and threads" : "resize columns"}
      onPointerDown={onPointerDown}
    >
      <span className="resize-handle-bar" />
    </div>
  );
}

/**
 * The full-width top area: the pinned pair (My Tasks | Follow-ups) under ONE shared
 * DndContext so a task can be dragged between them, the Threads panel spanning both
 * pinned columns in a second row (goal 14), and the scratchpad as the right column
 * spanning both rows — all in a single resizable grid. A missing pinned title
 * degrades to an empty-column hint without affecting the other columns. Threads and
 * the scratchpad are passed in (composed by DashboardPage) so this panel imports no
 * sibling panel.
 */
export function PinnedTasksRow({
  tasks,
  scratchpad,
  threads,
  threadLinks,
  onOpenThread,
}: {
  tasks: TasksHook;
  scratchpad: ReactNode;
  threads?: ReactNode;
  threadLinks?: Map<string, ThreadRef>;
  onOpenThread?: (threadId: number) => void;
}) {
  const { taskLists, isLoading, error } = tasks;
  const allLists = allListRefs(taskLists);
  const [widths, setWidths] = useState(DEFAULT_WIDTHS);
  const [rowSplit, setRowSplit] = useState(DEFAULT_ROW_SPLIT);
  const containerRef = useRef<HTMLDivElement>(null);

  const resolved = PINNED_LIST_TITLES.map((title) => ({
    title,
    list: taskLists.find((l) => l.title === title) ?? null,
  }));
  const foundLists = resolved
    .map((r) => r.list)
    .filter((l): l is TaskList => l !== null);

  // Drag a handle: convert pointer dx to a fraction of the flexible width and
  // shift it between the two columns the handle sits between (clamped to MIN).
  function beginResize(boundary: number, e: ReactPointerEvent<HTMLDivElement>) {
    e.preventDefault();
    const container = containerRef.current;
    if (!container) return;
    const avail = container.clientWidth - 2 * HANDLE_PX;
    if (avail <= 0) return;
    const startX = e.clientX;
    const start = widths;
    const li = boundary;
    const ri = boundary + 1;
    function onMove(ev: PointerEvent) {
      const d = (ev.clientX - startX) / avail;
      let left = start[li] + d;
      let right = start[ri] - d;
      if (left < MIN_FRAC) {
        right -= MIN_FRAC - left;
        left = MIN_FRAC;
      }
      if (right < MIN_FRAC) {
        left -= MIN_FRAC - right;
        right = MIN_FRAC;
      }
      const next = [...start];
      next[li] = left;
      next[ri] = right;
      setWidths(next);
    }
    function onUp() {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.style.userSelect = "";
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    document.body.style.userSelect = "none";
  }

  // Drag the row handle: pointer dy as a fraction of the height left after the
  // handle row shifts height between the task columns and Threads.
  function beginRowResize(e: ReactPointerEvent<HTMLDivElement>) {
    e.preventDefault();
    const container = containerRef.current;
    if (!container) return;
    const avail = container.clientHeight - HANDLE_PX;
    if (avail <= 0) return;
    const startY = e.clientY;
    const start = rowSplit;
    function onMove(ev: PointerEvent) {
      const next = start + (ev.clientY - startY) / avail;
      setRowSplit(Math.min(1 - MIN_ROW_FRAC, Math.max(MIN_ROW_FRAC, next)));
    }
    function onUp() {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    document.body.style.userSelect = "none";
    document.body.style.cursor = "row-resize";
  }

  const gridStyle = {
    "--r0": `${rowSplit}fr`,
    "--r1": `${1 - rowSplit}fr`,
    "--w0": `${widths[0]}fr`,
    "--w1": `${widths[1]}fr`,
    "--w2": `${widths[2]}fr`,
    "--handle": `${HANDLE_PX}px`,
  } as CSSProperties;

  // The two pinned slots, in title order: a real column (found) or a hint.
  let slot0: ReactNode;
  let slot1: ReactNode;
  if (isLoading || error) {
    slot0 = (
      <section className="panel task-column pinned-slot-0">
        <p className={`panel-status${error ? " panel-error" : ""}`}>
          {error ?? "Loading…"}
        </p>
      </section>
    );
    slot1 = <section className="panel task-column pinned-slot-1" aria-hidden />;
  } else {
    const [s0, s1] = resolved.map(({ title, list }, i) =>
      list ? (
        <TaskListColumn
          key={title}
          list={list}
          allLists={allLists}
          tasks={tasks}
          compactDates
          className={`pinned-slot-${i}`}
          threadLinks={threadLinks}
          onOpenThread={onOpenThread}
        />
      ) : (
        <section
          key={title}
          className={`panel task-column pinned-missing pinned-slot-${i}`}
        >
          <div className="panel-head">
            <h2 className="list-title">{title}</h2>
          </div>
          <p className="panel-status panel-error">
            list &lsquo;{title}&rsquo; not found — rename a list to match,
            or edit PINNED_LIST_TITLES.
          </p>
        </section>
      ),
    );
    slot0 = s0;
    slot1 = s1;
  }

  return (
    <div className="top-row-resizable" ref={containerRef} style={gridStyle}>
      <DndListGroup lists={foundLists} tasks={tasks}>
        {slot0}
        <ResizeHandle
          className="resize-handle--pair"
          onPointerDown={(e) => beginResize(0, e)}
        />
        {slot1}
      </DndListGroup>
      {/* DOM order is the stacked (≤1080px) order: My Tasks, Follow-ups, Threads,
          Scratchpad. The wide grid places each item explicitly (see CSS). */}
      {threads && (
        <ResizeHandle
          horizontal
          className="resize-handle--rows"
          onPointerDown={beginRowResize}
        />
      )}
      {threads}
      <ResizeHandle
        className="resize-handle--scratch"
        onPointerDown={(e) => beginResize(1, e)}
      />
      {scratchpad}
    </div>
  );
}

/**
 * The shared write toasts (action-undo + error). Rendered once for the whole
 * tasks surface — both `.toast` variants are `position: fixed`, so a single
 * mount covers every column. State comes from the one lifted `useTasksPanel`.
 */
export function TasksToasts({ tasks }: { tasks: TasksHook }) {
  const {
    writeError,
    actionToast,
    dismissWriteError,
    undoActionToast,
    runToastAction,
  } = tasks;

  // Auto-dismiss the error toast after ~4s. (The action toast self-expires in
  // the hook after ~5s, committing any deferred write.)
  useEffect(() => {
    if (!writeError) return;
    const id = window.setTimeout(dismissWriteError, 4000);
    return () => window.clearTimeout(id);
  }, [writeError, dismissWriteError]);

  return (
    <>
      {actionToast && (
        <div className="toast toast--action" role="status">
          <span>{actionToast.message}</span>
          {actionToast.actionLabel && (
            <button className="toast-undo" onClick={runToastAction}>
              {actionToast.actionLabel}
            </button>
          )}
          <button className="toast-undo" onClick={undoActionToast}>
            Undo
          </button>
        </div>
      )}
      {writeError && (
        <div className="toast" role="alert">
          <span>{writeError}</span>
          <button
            className="toast-dismiss"
            aria-label="dismiss"
            onClick={dismissWriteError}
          >
            ×
          </button>
        </div>
      )}
    </>
  );
}
