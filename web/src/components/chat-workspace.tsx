"use client";

import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  type ChatAction,
  type ChatMessage,
  type Citation,
  type Conversation,
  type SlotOption,
  type SlotsCard,
  chooseSlot,
  createConversation,
  fetchConversations,
  fetchMessages,
  fetchSuggestion,
  removeConversation,
  resolveAction,
  streamChatMessage,
} from "@/lib/api";
import type { Tenant } from "@/lib/tenants";
import { ChatCards } from "@/components/chat-cards";
import { MyActivity } from "@/components/my-activity";

const CONVERSATION_GONE = "Conversation not found";

let localMessages = 0;
function localMessageId(kind: string) {
  localMessages += 1;
  return `${kind}-${localMessages}`;
}

const ACTION_OUTCOME: Record<ChatAction["status"], string> = {
  pending: "",
  confirmed: "Confirmed",
  cancelled: "Not carried out",
  expired: "Expired, please ask again",
};

function ActionCard({ action, disabled, onResolve }: {
  action: ChatAction;
  disabled: boolean;
  onResolve: (action: ChatAction, decision: "confirm" | "dismiss") => void;
}) {
  const { title, lines, total, note } = action.summary;
  return (
    <section className={`action-card ${action.status}`} aria-label={title}>
      <h3>{title}</h3>
      {lines.map((line) => <div className="action-line" key={line.label}><span>{line.label}</span><strong>{line.value}</strong></div>)}
      {total && <div className="action-line action-total"><span>Total</span><strong>{total}</strong></div>}
      {note && <p className="action-note">{note}</p>}
      {action.status === "pending"
        ? <div className="actions"><button className="button primary" disabled={disabled} onClick={() => onResolve(action, "confirm")}>Confirm</button><button className="button" disabled={disabled} onClick={() => onResolve(action, "dismiss")}>Not now</button></div>
        : <p className="action-outcome">{ACTION_OUTCOME[action.status]}</p>}
    </section>
  );
}

function Reply({ text }: { text: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></div>;
}

function Citations({ citations }: { citations: Citation[] }) {
  const [openKey, setOpenKey] = useState("");
  const open = citations.find((citation) => `${citation.document}-${citation.section}` === openKey);
  return (
    <div className="citations">
      <div className="citation-list">
        {citations.map((citation) => {
          const key = `${citation.document}-${citation.section}`;
          return (
            <button aria-expanded={key === openKey} key={key} onClick={() => setOpenKey(key === openKey ? "" : key)}>
              {citation.document} › {citation.section}
            </button>
          );
        })}
      </div>
      {open && (
        <blockquote className="citation-source">
          <strong>{open.document} › {open.section}</strong>
          <p>{open.content || "The source text isn't saved for this older message."}</p>
        </blockquote>
      )}
    </div>
  );
}

export function ChatWorkspace({ tenant }: { tenant: Tenant }) {
  const [suggestion, setSuggestion] = useState("");
  const composer = useRef<HTMLTextAreaElement>(null);
  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [activity, setActivity] = useState("");
  const stopper = useRef<AbortController | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [activityVersion, setActivityVersion] = useState(0);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Async handlers outlive the render that started them, so they read the open chat from here.
  const openId = useRef("");
  const thread = useRef<HTMLDivElement>(null);

  useEffect(() => {
    thread.current?.scrollTo({ top: thread.current.scrollHeight });
  }, [messages, busy, streamingText, activity]);

  const openConversation = useCallback((id: string) => {
    openId.current = id;
    setActiveId(id);
    setMessages([]);
    setError("");
    setSuggestion("");
  }, []);

  /** Runs after a reply finishes, so a slow or failed guess can't hold anything up. */
  const offerSuggestion = useCallback(async (conversationId: string) => {
    const next = await fetchSuggestion(conversationId);
    if (!next || openId.current !== conversationId) return;
    setSuggestion(next);
    // Tab only works from the message box, but opening a phone keyboard uninvited would be rude.
    if (window.matchMedia("(pointer: fine)").matches && document.activeElement === document.body) composer.current?.focus();
  }, []);

  const forgetConversation = useCallback((id: string) => {
    setConversations((current) => current.filter((conversation) => conversation.id !== id));
    if (openId.current === id) openConversation("");
  }, [openConversation]);

  const selectConversation = useCallback(async (id: string) => {
    openConversation(id);
    try {
      const rows = await fetchMessages(id);
      if (openId.current === id) setMessages(rows);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Could not load messages";
      if (message === CONVERSATION_GONE) forgetConversation(id);
      else if (openId.current === id) setError(message);
    }
  }, [openConversation, forgetConversation]);

  useEffect(() => {
    fetchConversations()
      .then(async (rows) => {
        setConversations(rows);
        if (rows[0]) await selectConversation(rows[0].id);
      })
      .catch((reason) => setError(reason.message));
  }, [selectConversation]);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    await send(draft);
  }

  async function send(text: string) {
    const content = text.trim();
    if (!content || busy) return;
    setSuggestion("");
    const optimistic: ChatMessage = {
      id: localMessageId("pending"),
      role: "user",
      content,
      citations: [],
      created_at: new Date().toISOString(),
      actions: [],
    };
    setMessages((current) => [...current, optimistic]);
    setDraft("");
    setBusy(true);
    setError("");
    setActivity("");
    setStreamingText("");
    const controller = new AbortController();
    stopper.current = controller;
    setStreaming(true);
    let conversationId = openId.current;
    let partial = "";
    let answered = false;
    try {
      // A chat is only stored once it has a first message, so empty ones never pile up.
      if (!conversationId) {
        const conversation = await createConversation(content.slice(0, 60));
        conversationId = conversation.id;
        openId.current = conversationId;
        setActiveId(conversationId);
        setConversations((current) => [conversation, ...current]);
      }
      await streamChatMessage(conversationId, content, controller.signal, (event) => {
        if (event.type === "text") {
          partial += event.delta;
          setStreamingText(partial);
          setActivity("");
        } else if (event.type === "text_reset") {
          partial = "";
          setStreamingText("");
        } else if (event.type === "activity") {
          setActivity(event.label);
        } else if (event.type === "done") {
          partial = "";
          answered = true;
          setMessages((current) => [...current, event.message]);
        } else {
          partial = "";
          setError(event.detail);
        }
      });
    } catch (reason) {
      if (controller.signal.aborted) {
        // The server saves the same partial reply, so a reload shows what's on screen now.
        if (partial.trim()) {
          const stopped: ChatMessage = { ...optimistic, id: localMessageId("stopped"), role: "assistant", content: partial.trim() };
          setMessages((current) => [...current, stopped]);
        }
        return;
      }
      const message = reason instanceof Error ? reason.message : "The agent could not respond";
      setMessages((current) => current.filter((item) => item.id !== optimistic.id));
      setDraft(content);
      if (message === CONVERSATION_GONE) {
        forgetConversation(conversationId);
        setError("That chat was deleted. Send your message again to start a new one.");
      } else {
        setError(message);
      }
    } finally {
      stopper.current = null;
      setStreaming(false);
      setStreamingText("");
      setActivity("");
      setBusy(false);
      if (answered) void offerSuggestion(conversationId);
    }
  }

  async function pickSlot(card: SlotsCard, slot: SlotOption) {
    const conversationId = openId.current;
    setBusy(true);
    setError("");
    try {
      const { question, message } = await chooseSlot(conversationId, card, slot);
      if (openId.current === conversationId) setMessages((current) => [...current, question, message]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not choose that time");
    } finally {
      setBusy(false);
    }
  }

  async function resolve(action: ChatAction, decision: "confirm" | "dismiss") {
    const conversationId = openId.current;
    setBusy(true);
    setError("");
    try {
      const result = await resolveAction(conversationId, action.id, decision);
      if (openId.current === conversationId) {
        setMessages((current) => [
          ...current.map((message) => ({
            ...message,
            actions: message.actions.map((item) => (item.id === action.id ? result.action : item)),
          })),
          result.message,
        ]);
      }
      if (result.action.status === "confirmed") setActivityVersion((version) => version + 1);
      void offerSuggestion(conversationId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not complete that request");
      await selectConversation(conversationId);
    } finally {
      setBusy(false);
    }
  }

  function composerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Tab" && !event.shiftKey && !draft && suggestion) {
      event.preventDefault();
      acceptSuggestion();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  function acceptSuggestion() {
    setDraft(suggestion);
    setSuggestion("");
    composer.current?.focus();
  }

  async function remove(id: string) {
    try {
      await removeConversation(id);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Could not delete the chat";
      if (message !== CONVERSATION_GONE) {
        setError(message);
        return;
      }
    }
    forgetConversation(id);
  }

  return (
    <section className="chat-shell">
      <aside className={sidebarOpen ? "conversation-sidebar open" : "conversation-sidebar"}>
        <button className="button primary" disabled={busy} onClick={() => { openConversation(""); setSidebarOpen(false); }}>New chat</button>
        <div className="conversation-list">
          {conversations.map((conversation) => (
            <div className={conversation.id === activeId ? "conversation active" : "conversation"} key={conversation.id}>
              <button disabled={busy} onClick={() => { void selectConversation(conversation.id); setSidebarOpen(false); }}>{conversation.title}</button>
              <button aria-label={`Delete ${conversation.title}`} disabled={busy} onClick={() => remove(conversation.id)}>×</button>
            </div>
          ))}
        </div>
        <MyActivity tenant={tenant} version={activityVersion} />
      </aside>
      <div className="chat-panel">
        <button className="sidebar-toggle" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen((open) => !open)}>
          {sidebarOpen ? "Close menu" : `Chats and my ${tenant.kind === "store" ? "orders" : "bookings"}`}
        </button>
        <div className="message-thread" aria-live="polite" ref={thread}>
          {!messages.length && (
            <div className="chat-empty">
              <p className="chat-greeting">{tenant.greeting}</p>
              <div className="chat-suggestions">
                {tenant.suggestions.map((suggestion) => <button key={suggestion} disabled={busy} onClick={() => void send(suggestion)}>{suggestion}</button>)}
              </div>
            </div>
          )}
          {messages.map((message) => (
            <article className={`chat-message ${message.role}`} key={message.id}>
              {message.role === "assistant" ? <Reply text={message.content} /> : <p>{message.content}</p>}
              {!!message.citations.length && <Citations citations={message.citations} />}
              <ChatCards cards={message.cards ?? []} disabled={busy} onPick={pickSlot} />
              {(message.actions ?? []).map((action) => <ActionCard action={action} disabled={busy} key={action.id} onResolve={resolve} />)}
            </article>
          ))}
          {streamingText && <article className="chat-message assistant"><Reply text={streamingText} /></article>}
          {busy && !streamingText && <p className="tool-activity" role="status">{activity || "Thinking…"}</p>}
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        {suggestion && !draft && (
          <button className="next-suggestion" onClick={acceptSuggestion} type="button">
            <kbd>Tab</kbd><span>{suggestion}</span>
          </button>
        )}
        <form className="composer" onSubmit={submit}>
          <textarea ref={composer} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={composerKeyDown} placeholder={suggestion || tenant.placeholder} aria-label="Message" readOnly={busy} />
          {streaming
            ? <button className="button" type="button" onClick={() => stopper.current?.abort()}>Stop</button>
            : <button className="button primary" disabled={busy || !draft.trim()}>Send</button>}
        </form>
      </div>
    </section>
  );
}
