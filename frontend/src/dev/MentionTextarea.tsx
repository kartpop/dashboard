import { useCallback, useRef, useState } from "react";
import {
  type Member,
  type MentionQuery,
  filterMembers,
  insertMention,
  mentionQuery,
} from "./mentions";

/**
 * The draft-body editor with an @-mention typeahead (goal 12b). Typing `@` opens a
 * small dropdown of the repo's assignable logins; picking one inserts plain `@login`
 * text at the caret — that's the whole mechanism (GitHub linkifies and notifies once
 * filed; there is no new write surface). Members are fetched lazily on the first `@`
 * via `loadMembers` (cached per repo by the caller); an empty list simply offers
 * nothing — typing a login by hand still works, it's just text.
 */
export function MentionTextarea({
  value,
  onChange,
  onBlur,
  rows,
  placeholder,
  loadMembers,
}: {
  value: string;
  onChange: (text: string) => void;
  onBlur: () => void;
  rows: number;
  placeholder: string;
  loadMembers: () => Promise<Member[]>;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const members = useRef<Member[] | null>(null);
  const [menu, setMenu] = useState<{
    mention: MentionQuery;
    items: Member[];
    active: number;
  } | null>(null);

  /** Re-evaluate the mention under the caret (on every edit and caret move). */
  const refresh = useCallback(
    (text: string, caret: number) => {
      const mention = mentionQuery(text, caret);
      if (!mention) {
        setMenu(null);
        return;
      }
      const offer = (list: Member[]) => {
        const items = filterMembers(list, mention.query);
        setMenu(items.length ? { mention, items, active: 0 } : null);
      };
      if (members.current) {
        offer(members.current);
      } else {
        void loadMembers().then((list) => {
          members.current = list;
          offer(list);
        });
      }
    },
    [loadMembers],
  );

  const pick = (login: string) => {
    if (!menu || !ref.current) return;
    const caret = ref.current.selectionStart;
    const out = insertMention(value, menu.mention, caret, login);
    onChange(out.text);
    setMenu(null);
    // Restore the caret after React re-renders the textarea value.
    requestAnimationFrame(() => {
      ref.current?.setSelectionRange(out.caret, out.caret);
      ref.current?.focus();
    });
  };

  return (
    <div className="dev-mention-wrap">
      <textarea
        ref={ref}
        className="dev-card-body"
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          refresh(e.target.value, e.target.selectionStart);
        }}
        onSelect={(e) => refresh(value, e.currentTarget.selectionStart)}
        onKeyDown={(e) => {
          if (!menu) return;
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            const delta = e.key === "ArrowDown" ? 1 : -1;
            setMenu({
              ...menu,
              active:
                (menu.active + delta + menu.items.length) % menu.items.length,
            });
          } else if (e.key === "Enter" || e.key === "Tab") {
            e.preventDefault();
            pick(menu.items[menu.active].login);
          } else if (e.key === "Escape") {
            setMenu(null);
          }
        }}
        onBlur={() => {
          // A dropdown click lands as mousedown before blur — the picks below use
          // onMouseDown, so closing here never races an insert.
          setMenu(null);
          onBlur();
        }}
        rows={rows}
        placeholder={placeholder}
      />
      {menu && (
        <ul className="dev-mention-menu" role="listbox">
          {menu.items.map((m, i) => (
            <li key={m.login}>
              <button
                type="button"
                role="option"
                aria-selected={i === menu.active}
                className={`dev-mention-item${i === menu.active ? " dev-mention-item--active" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault(); // keep focus in the textarea
                  pick(m.login);
                }}
              >
                @{m.login}
                {m.name ? (
                  <span className="dev-mention-name">{m.name}</span>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
