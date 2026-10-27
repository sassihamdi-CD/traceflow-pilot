"use client";
import { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { FileUp, Loader2 } from "lucide-react";
import { clsx } from "clsx";

const ACCEPT =
  ".pdf,.docx,.doc,.xlsx,.xlsm,.xls,.csv,.tsv,.ods,.odt,.pptx,.odp,.eml,.txt,.md,.markdown,.log,.rtf,.html,.htm,.xml,.json,.png,.jpg,.jpeg,.webp,.gif,.tif,.tiff,.bmp";
const SUPPORTED = /\.(pdf|docx|doc|xlsx|xlsm|xls|csv|tsv|ods|odt|pptx|odp|eml|txt|md|markdown|log|rtf|html?|xml|json|png|jpe?g|webp|gif|tiff?|bmp)$/i;

/** Ledger-styled dropzone. Multi-file; rejects unknown types client-side before upload. */
export function UploadDropzone({ onFiles, busy }: { onFiles: (fs: File[]) => void; busy: boolean }) {
  const t = useTranslations("console.upload");
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [rejected, setRejected] = useState("");
  function take(list: FileList | File[] | undefined) {
    const files = Array.from(list ?? []);
    if (files.length === 0) return;
    const bad = files.filter((f) => !SUPPORTED.test(f.name));
    const good = files.filter((f) => SUPPORTED.test(f.name));
    setRejected(bad.length > 0 ? t("rejected", { names: bad.map((f) => `"${f.name}"`).join(", ") }) : "");
    if (good.length > 0) onFiles(good);
  }
  return (
    <div>
      <button
        type="button"
        disabled={busy}
        onClick={() => input.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); take(e.dataTransfer.files); }}
        className={clsx(
          "flex w-full items-center gap-4 rounded-xl2 border border-dashed bg-parchment/60 px-5 py-4 text-left shadow-card transition-colors duration-150",
          drag ? "border-ink bg-parchment" : "border-muted/50 hover:border-ink",
        )}
      >
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-ink text-paper">
          {busy ? <Loader2 size={20} className="animate-spin" /> : <FileUp size={20} />}
        </span>
        <span>
          <span className="block text-sm font-semibold">
            {busy ? t("extracting") : t("drop")}
          </span>
          <span className="block font-mono text-xs text-muted">{t("hint")}</span>
        </span>
      </button>
      <input
        ref={input}
        type="file"
        accept={ACCEPT}
        multiple
        className="hidden"
        onChange={(e) => { take(e.target.files ?? undefined); e.target.value = ""; }}
      />
      {rejected && <p className="mt-2 text-sm font-medium text-brick">{rejected}</p>}
    </div>
  );
}
