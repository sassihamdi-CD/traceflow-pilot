"use client";
import React, { useState } from "react";
import { Bot, SendHorizonal, UserRound } from "lucide-react";
import { clsx } from "clsx";
import { apiAuthed } from "@/lib/api";
import { useTranslations } from "next-intl";

type Msg = { role: "you" | "ai"; text: string };

function FormattedMessage({ text }: { text: string }) {
  const lines = text.split("\n");
  const blocks: React.ReactNode[] = [];
  let currentList: React.ReactNode[] = [];

  const flushList = () => {
    if (currentList.length > 0) {
      blocks.push(
        <ul key={`ul-${blocks.length}`} className="my-2 space-y-1.5 pl-1">
          {currentList}
        </ul>
      );
      currentList = [];
    }
  };

  const formatInline = (str: string): React.ReactNode[] => {
    const regex = /(\[[^\]\n]+→[^\]\n]+\]|\*\*[^*\n]+\*\*)/g;
    const parts = str.split(regex);

    return parts.map((part, idx) => {
      if (!part) return null;
      if (part.startsWith("[") && part.endsWith("]") && part.includes("→")) {
        return (
          <span
            key={idx}
            className="mx-1 inline-flex items-center rounded-md border border-line bg-parchment/60 px-1.5 py-0.5 font-mono text-[11px] text-inksoft shadow-xs"
          >
            {part.slice(1, -1)}
          </span>
        );
      }
      if (part.startsWith("**") && part.endsWith("**")) {
        return (
          <strong key={idx} className="font-semibold text-ink">
            {part.slice(2, -2)}
          </strong>
        );
      }
      return <span key={idx}>{part}</span>;
    });
  };

  lines.forEach((rawLine, idx) => {
    const line = rawLine.trim();

    if (!line) {
      flushList();
      return;
    }

    const headingMatch = line.match(/^#{1,4}\s+(.*)$/);
    if (headingMatch) {
      flushList();
      blocks.push(
        <h4 key={`h-${idx}`} className="mt-3.5 mb-1.5 text-[15px] font-semibold text-ink">
          {formatInline(headingMatch[1])}
        </h4>
      );
      return;
    }

    const bulletMatch = line.match(/^[-*•]\s+(.*)$/);
    if (bulletMatch) {
      currentList.push(
        <li key={`li-${idx}`} className="flex items-start gap-2 text-[14px] leading-relaxed text-ink">
          <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-ink/50" />
          <span className="flex-1">{formatInline(bulletMatch[1])}</span>
        </li>
      );
      return;
    }

    flushList();
    blocks.push(
      <p key={`p-${idx}`} className="my-1.5 text-[14px] leading-relaxed text-ink">
        {formatInline(line)}
      </p>
    );
  });

  flushList();

  return <div className="space-y-1">{blocks}</div>;
}

/** Ask your data: authenticated Q&A over the workspace snapshot.
 * Answers cite [SKU → field → document / location] and always separate
 * verified values from proposed ones. */
export default function AskPage() {
  const t = useTranslations("console.ask");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const suggestions = [
    t("suggestion1"),
    t("suggestion2"),
    t("suggestion3"),
  ];

  async function send(text: string) {
    const q = text.trim();
    if (!q || busy) return;
    setError("");
    setMsgs((m) => [...m, { role: "you", text: q }]);
    setInput("");
    setBusy(true);
    try {
      const out = await apiAuthed("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      }, 90_000);
      setMsgs((m) => [...m, { role: "ai", text: out.answer }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("askFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <p className="font-mono text-xs uppercase tracking-[0.14em] text-muted">{t("headerBadge")}</p>
      <h1 className="mt-1 font-display text-4xl font-semibold tracking-tight">{t("title")}</h1>
      <p className="mt-2 max-w-xl text-sm text-inksoft">
        {t("subtitle")}
      </p>

      {msgs.length === 0 && (
        <div className="mt-5 flex flex-wrap gap-2">
          {suggestions.map((s) => (
            <button key={s} onClick={() => void send(s)}
              className="min-h-[40px] rounded-xl border border-line bg-white/70 px-4 text-left text-sm font-medium transition-all hover:scale-[1.01] hover:border-ink">
              {s}
            </button>
          ))}
        </div>
      )}

      <div className="mt-5 space-y-4">
        {msgs.map((m, i) => (
          <div key={i} className={clsx("flex gap-3", m.role === "you" ? "justify-end" : "justify-start")}>
            {m.role === "ai" && (
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-ink text-paper">
                <Bot size={16} />
              </span>
            )}
            <div className={clsx(
              "max-w-[80%] rounded-xl2 px-4 py-3 shadow-card",
              m.role === "you" ? "bg-ink text-paper text-[15px] leading-relaxed whitespace-pre-wrap" : "border border-line bg-white/90 text-ink",
            )}>
              {m.role === "ai" ? <FormattedMessage text={m.text} /> : m.text}
            </div>
            {m.role === "you" && (
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-parchment text-ink">
                <UserRound size={16} />
              </span>
            )}
          </div>
        ))}
        {busy && (
          <div className="flex gap-3">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-ink text-paper">
              <Bot size={16} />
            </span>
            <div className="animate-pulse rounded-xl2 border border-line bg-white/80 px-4 py-3 text-sm text-muted">
              {t("thinking")}
            </div>
          </div>
        )}
      </div>

      {error && <p className="mt-3 text-sm font-medium text-brick">{error}</p>}

      <form onSubmit={(e) => { e.preventDefault(); void send(input); }} className="sticky bottom-4 mt-5 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={t("placeholder")}
          maxLength={2000}
          className="min-h-[48px] flex-1 rounded-xl border border-line bg-white/90 px-4 text-[15px] shadow-card outline-none transition-shadow placeholder:text-muted/70 focus:border-ink focus:ring-2 focus:ring-ink/15"
        />
        <button type="submit" disabled={busy || !input.trim()}
          className="inline-flex min-h-[48px] items-center gap-2 rounded-xl bg-ink px-5 font-semibold text-paper transition-transform duration-150 hover:scale-[1.02] active:scale-[0.98] disabled:opacity-50">
          <SendHorizonal size={17} /> {t("askButton")}
        </button>
      </form>
    </>
  );
}
