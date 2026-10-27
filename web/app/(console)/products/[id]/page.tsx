"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Check, Copy, ExternalLink, FileText, Pencil, Rocket, Send, X } from "lucide-react";
import { clsx } from "clsx";
import { useTranslations } from "next-intl";
import { apiAuthed, apiUploadDocument } from "@/lib/api";
import { FIELD_DESCRIPTIONS, draftSupplierEmail, formatSignature, loadSignature } from "@/lib/fieldMeta";
import { EmailDraftModal } from "@/components/EmailDraftModal";
import { DocumentItems } from "@/components/DocumentItems";
import { StateBadge } from "@/components/StateBadge";
import { ReadinessRing } from "@/components/ReadinessRing";
import { UploadDropzone } from "@/components/UploadDropzone";

type Value = { id: string; field_key: string; value: string; unit: string | null; status: string; document_id: string | null; location: string | null; extracted_by?: string | null };
type Field = { field_key: string; label: string; required: boolean; state: string; has_new_proposal: boolean; values: Value[] };

const COUNT_STYLE = [
  ["verified", "bg-moss"],
  ["proposed", "bg-amber"],
  ["missing", "bg-muted/60"],
  ["conflicting", "bg-brick"],
] as const;

function ActionButton({ onClick, icon: Icon, label, tone }: { onClick: () => void; icon: typeof Check; label: string; tone: string }) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        "inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border px-3 text-[13px] font-semibold transition-all duration-150 hover:scale-[1.02] active:scale-[0.97]",
        tone,
      )}
    >
      <Icon size={14} strokeWidth={2.5} /> {label}
    </button>
  );
}

export default function ProductDetail({ params }: { params: { id: string } }) {
  const t = useTranslations("console.product");
  const tMeta = useTranslations("console.fieldMeta");

  const [detail, setDetail] = useState<any>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [edit, setEdit] = useState<Record<string, string>>({});
  const [uploading, setUploading] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameForm, setRenameForm] = useState({ name: "", sku: "", manufacturer_name: "" });
  const [suppliers, setSuppliers] = useState<any[]>([]);
  const [askSupplier, setAskSupplier] = useState<Record<string, string>>({});
  const [mailDraft, setMailDraft] = useState<{ subject: string; body: string; hasSignature: boolean } | null>(null);
  const [lastDraft, setLastDraft] = useState<{ label: string; fieldKey: string; recipient: string; state: string } | null>(null);
  const [newSupplier, setNewSupplier] = useState("");
  const [manual, setManual] = useState<Record<string, string>>({});
  const [savingManual, setSavingManual] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [replace, setReplace] = useState<Record<string, string>>({});
  const [replaceOpen, setReplaceOpen] = useState<Record<string, boolean>>({});
  const [savingReplace, setSavingReplace] = useState<string | null>(null);

  async function quickAddSupplier(): Promise<string | null> {
    const name = newSupplier.trim();
    if (!name) {
      setError(t("supplierNameFirst"));
      return null;
    }
    setError("");
    try {
      const created = await apiAuthed("/api/suppliers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const fresh = await apiAuthed("/api/suppliers");
      setSuppliers(fresh);
      setNewSupplier("");
      setNotice(t("supplierAdded", { name }));
      return created.id as string;
    } catch (err) {
      setError(err instanceof Error ? err.message : t("addFailed"));
      return null;
    }
  }

  const load = useCallback(async () => {
    try {
      setDetail(await apiAuthed(`/api/products/${params.id}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("loadFailed"));
    }
  }, [params.id, t]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    apiAuthed("/api/suppliers").then(setSuppliers).catch(() => {});
  }, []);

  // Auto-refresh while any document is still extracting — the reviewer never
  // polls manually. Stops the moment every document reports done/failed.
  useEffect(() => {
    const pending = (detail?.documents ?? []).some((d: any) => d.extraction_status === "pending" || d.extraction_status === "extracting");
    if (!pending) return;
    const tTimer = setInterval(() => { void load(); }, 4000);
    return () => clearInterval(tTimer);
  }, [detail, load]);

  async function act(path: string, body?: unknown, okMsg?: string) {
    setError(""); setNotice("");
    try {
      await apiAuthed(path, {
        method: "POST",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      if (okMsg) setNotice(okMsg);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("actionFailed"));
    }
  }

  async function uploadFiles(files: File[]) {
    setUploading(true); setError(""); setNotice("");
    try {
      let ok = 0, fail = 0;
      let firstError = "";
      for (const file of files) {
        try {
          await apiUploadDocument(params.id, file);
          ok += 1;
        } catch (err) {
          fail += 1;
          if (!firstError) {
            firstError = err instanceof Error
              ? t("uploadFailed", { message: err.message })
              : t("uploadFailedFallback");
          }
        }
      }
      if (fail > 0) setError(`${t("uploadsFailed", { fail, ok })}${firstError ? ` ${firstError}` : ""}`);
      else setNotice(t("uploadsOk", { ok }));
      await load();
    } finally {
      setUploading(false);
    }
  }

  // The system picks the supplier itself: whoever backs this dossier's documents.
  // Exactly one linked supplier → used silently. Otherwise the reviewer picks.
  function dossierSupplierIds(): string[] {
    const ids = new Set<string>();
    for (const d of detail?.documents ?? []) {
      if (d.supplier_id) ids.add(d.supplier_id);
    }
    return Array.from(ids);
  }

  function supplierForField(fieldKey: string): string | undefined {
    if (askSupplier[fieldKey]) return askSupplier[fieldKey];
    const linked = dossierSupplierIds();
    if (linked.length === 1) return linked[0];
    return undefined;
  }

  async function requestFromSupplier(fieldKey: string, label: string) {
    const supplierId = supplierForField(fieldKey);
    if (!supplierId) {
      setError(t("noSupplierYet"));
      return;
    }
    setError(""); setNotice("");
    try {
      const sup = suppliers.find((s) => s.id === supplierId);
      await apiAuthed(`/api/suppliers/${supplierId}/followups`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product_id: params.id, field_key: fieldKey }),
      });
      setLastDraft({
        label,
        fieldKey,
        recipient: sup?.name ?? t("supplierFallback"),
        state: fields.find((f: Field) => f.field_key === fieldKey)?.state ?? "missing",
      });
      setNotice(t("requestSaved", { label }));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("requestFailed"));
    }
  }

  function openGapEmail(fieldKey: string, label: string, state: string) {
    const supplierId = supplierForField(fieldKey);
    const sup = suppliers.find((s) => s.id === supplierId);
    const meta = FIELD_DESCRIPTIONS[fieldKey];
    const note =
      state === "conflicting"
        ? "Our sources disagree — please confirm the authoritative value and its source."
        : `Nothing in our documents covers this yet — usually proven by: ${meta?.proof ?? "a supplier document"}.`;
    const sig = loadSignature();
    const { subject, body } = draftSupplierEmail({
      supplierName: sup?.name ?? t("supplierFallback"),
      productName: detail.product.name,
      productSku: detail.product.sku,
      gaps: [{ field_key: fieldKey, label, note }],
      signature: sig,
    });
    setMailDraft({ subject, body, hasSignature: formatSignature(sig).length > 0 });
  }

  async function acceptAll() {
    setError(""); setNotice("");
    try {
      const out = await apiAuthed(`/api/products/${params.id}/accept-all`, { method: "POST" });
      const skipped = out.skipped_conflicts > 0 ? t("skippedConflicts", { count: out.skipped_conflicts }) : "";
      setNotice(t("acceptedN", { count: out.accepted.length }) + skipped);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("acceptAllFailed"));
    }
  }

  async function retryDocument(id: string) {
    setRetrying(id); setError(""); setNotice("");
    try {
      await apiAuthed(`/api/documents/${id}/retry`, { method: "POST" });
      setNotice(t("retryQueued"));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("actionFailed"));
    } finally {
      setRetrying(null);
    }
  }

  async function replaceVerified(fieldKey: string, label: string) {
    const value = (replace[fieldKey] ?? "").trim();
    if (!value) { setError(t("manualValueFirst")); return; }
    setSavingReplace(fieldKey); setError(""); setNotice("");
    try {
      await apiAuthed(`/api/products/${params.id}/fields/${fieldKey}/replace`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
      setNotice(t("replacedMsg", { label }));
      setReplaceOpen({ ...replaceOpen, [fieldKey]: false });
      setReplace({ ...replace, [fieldKey]: "" });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("actionFailed"));
    } finally {
      setSavingReplace(null);
    }
  }

  async function saveManual(fieldKey: string, label: string) {
    const value = (manual[fieldKey] ?? "").trim();
    if (!value) { setError(t("manualValueFirst")); return; }
    setSavingManual(fieldKey); setError(""); setNotice("");
    try {
      await apiAuthed(`/api/products/${params.id}/fields/${fieldKey}/manual`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
      setNotice(t("manualSavedMsg", { label }));
      setManual({ ...manual, [fieldKey]: "" });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("actionFailed"));
    } finally {
      setSavingManual(null);
    }
  }

  if (!detail) {
    return (
      <>
        <div className="mb-6">
          <Link
            href="/products"
            className="group inline-flex items-center gap-2 text-sm font-medium text-muted transition-colors hover:text-ink"
          >
            <ArrowLeft size={16} className="transition-transform duration-150 group-hover:-translate-x-1" />
            <span>{t("backToProducts")}</span>
          </Link>
        </div>
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-24 animate-pulse rounded-xl2 bg-parchment" />
          ))}
        </div>
        {error && <p className="mt-4 text-sm font-medium text-brick">{error}</p>}
      </>
    );
  }

  const { product, fields, counts } = detail;
  const published = product.passport_published;
  const missingFields = fields.filter((f: Field) => f.required && f.state === "missing");
  const unambiguous = fields.flatMap((f: Field) =>
    f.state === "proposed" ? f.values.filter((v) => v.status === "proposed") : [],
  );

  async function saveRename() {
    setError(""); setNotice("");
    const body = Object.fromEntries(Object.entries(renameForm).filter(([, v]) => v.trim()));
    if (Object.keys(body).length === 0) { setRenaming(false); return; }
    try {
      await apiAuthed(`/api/products/${params.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setNotice(t("renamed"));
      setRenaming(false);
      setRenameForm({ name: "", sku: "", manufacturer_name: "" });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("renameFailed"));
    }
  }

  return (
    <>
      <div className="mb-6">
        <Link
          href="/products"
          className="group inline-flex items-center gap-2 text-sm font-medium text-muted transition-colors hover:text-ink"
        >
          <ArrowLeft size={16} className="transition-transform duration-150 group-hover:-translate-x-1" />
          <span>{t("backToProducts")}</span>
        </Link>
      </div>

      <div className="grid gap-8 lg:grid-cols-[1fr_320px]">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.14em] text-muted">{product.sku} · {product.category}</p>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="mt-1 font-display text-4xl font-semibold tracking-tight">{product.name}</h1>
            <button onClick={() => setRenaming((v) => !v)}
              className="mt-1 inline-flex min-h-[32px] items-center gap-1 rounded-lg px-2 text-xs font-semibold text-muted transition-colors hover:bg-parchment hover:text-ink">
              <Pencil size={13} /> {t("rename")}
            </button>
          </div>
          <p className="mt-1 text-sm text-inksoft">{product.manufacturer_name}</p>
          {renaming && (
            <div className="mt-3 flex flex-wrap items-end gap-2 rounded-xl border border-line bg-white/70 p-3">
              {[["name", t("productName")], ["sku", t("sku")], ["manufacturer_name", t("manufacturer")]].map(([k, label]) => (
                <label key={k} className="text-xs font-semibold text-muted">
                  {label}
                  <input value={(renameForm as any)[k]}
                    onChange={(e) => setRenameForm({ ...renameForm, [k]: e.target.value })}
                    placeholder={(product as any)[k]}
                    className="mt-1 block min-h-[36px] w-44 rounded-lg border border-line bg-paper px-2.5 text-sm font-normal text-ink outline-none focus:border-ink" />
                </label>
              ))}
              <button onClick={() => void saveRename()}
                className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-ink px-4 text-[13px] font-semibold text-paper">
                <Check size={14} strokeWidth={2.5} /> {t("save")}
              </button>
            </div>
          )}
          {unambiguous.length > 0 && (
            <button onClick={() => void acceptAll()}
              className="mt-3 inline-flex min-h-[40px] items-center gap-2 rounded-xl bg-moss px-5 text-sm font-semibold text-white transition-transform duration-150 hover:scale-[1.01] active:scale-[0.99]">
              <Check size={16} strokeWidth={2.5} /> {t("acceptAll", { count: unambiguous.length })}
            </button>
          )}
          {fields.some((f: Field) => (f.field_key === "product_name" || f.field_key === "sku") && f.state === "proposed") && (
            <p className="mt-2 text-xs text-muted">{t("identityReviewHint")}</p>
          )}

          {(error || notice) && (
            <div className={clsx("mt-4 rounded-xl border p-3.5 text-sm font-medium", error ? "border-brick/30 bg-brick-wash text-brick" : "border-moss/30 bg-moss-wash text-moss-deep")}>
              {error || notice}
            </div>
          )}

          {missingFields.length > 0 && !published && (
            <div className="mt-4 rounded-xl border border-amber/30 bg-amber-wash/50 p-4">
              <p className="text-sm font-semibold text-amber">{t("missingBannerTitle", { count: missingFields.length })}</p>
              <p className="mt-1 text-[13px] leading-relaxed text-inksoft">{t("missingBannerBody")}</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {missingFields.map((f: Field) => {
                  const label = f.field_key in FIELD_DESCRIPTIONS ? tMeta(`${f.field_key}.label` as any) : f.label;
                  return (
                    <a key={f.field_key} href={`#field-${f.field_key}`}
                      className="rounded-full border border-amber/30 bg-paper px-2.5 py-1 text-xs font-semibold text-amber transition-colors hover:border-amber">
                      {label}
                    </a>
                  );
                })}
              </div>
              <p className="mt-2 text-xs text-amber/80">{t("missingBannerHint")}</p>
            </div>
          )}

          <div className="mt-6 space-y-3">
            {fields.map((f: Field, fi: number) => {
              const hasMeta = f.field_key in FIELD_DESCRIPTIONS;
              const fieldLabel = hasMeta ? tMeta(`${f.field_key}.label` as any) : f.label;
              const fieldWhat = hasMeta ? tMeta(`${f.field_key}.what` as any) : FIELD_DESCRIPTIONS[f.field_key]?.what;
              const fieldProof = hasMeta ? tMeta(`${f.field_key}.proof` as any) : FIELD_DESCRIPTIONS[f.field_key]?.proof;
              const verifiedValue = f.values.find((v) => v.status === "accepted" || v.status === "corrected")?.value ?? "";

              return (
                <section
                  key={f.field_key}
                  id={`field-${f.field_key}`}
                  style={{ animationDelay: `${Math.min(fi, 8) * 0.05}s` }}
                  className="animate-rise scroll-mt-24 rounded-xl2 border border-line bg-white/70 p-5 shadow-card"
                >
                  <div className="flex flex-wrap items-center gap-2.5">
                    <h2 className="font-display text-lg font-semibold">{fieldLabel}</h2>
                    <StateBadge state={f.state} pulse={f.state === "conflicting"} />
                    {f.has_new_proposal && (
                      <span className="rounded-full bg-amber-wash px-2.5 py-1 text-xs font-semibold text-amber">{t("newEvidence")}</span>
                    )}
                    <span className="ml-auto font-mono text-xs text-muted">{f.field_key}</span>
                  </div>
                  {fieldWhat && (
                    <p className="mt-1.5 text-[13px] leading-relaxed text-inksoft">
                      {fieldWhat}{" "}
                      <span className="text-muted">{t("usuallyProvenBy", { proof: fieldProof })}</span>
                    </p>
                  )}
                  {f.required && (f.state === "missing" || f.state === "conflicting") && (() => {
                    const autoId = supplierForField(f.field_key);
                    const autoSup = suppliers.find((s) => s.id === autoId);
                    const linkedCount = dossierSupplierIds().length;
                    return (
                    <div className="mt-2.5 rounded-xl border border-amber/30 bg-amber-wash/50 p-3">
                      <p className="text-[13px] font-medium text-amber">
                        {f.state === "missing"
                          ? t("gapMissing")
                          : t("gapConflict")}
                      </p>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        {suppliers.length === 0 ? (
                          <span className="inline-flex min-h-[36px] flex-1 items-center gap-2">
                            <input value={newSupplier}
                              onChange={(e) => setNewSupplier(e.target.value)}
                              placeholder={t("supplierNamePlaceholder")}
                              className="min-h-[36px] w-52 rounded-lg border border-line bg-paper px-2.5 text-[13px] outline-none focus:border-ink" />
                            <button onClick={() => void (async () => {
                              const id = await quickAddSupplier();
                              if (id) setAskSupplier({ ...askSupplier, [f.field_key]: id });
                            })()}
                              className="inline-flex min-h-[36px] items-center rounded-lg bg-ink px-3 text-[13px] font-semibold text-paper">
                              {t("addSupplier")}
                            </button>
                          </span>
                        ) : autoSup ? (
                          <span className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-paper px-3 text-[13px] font-semibold">
                            {t("to", { name: autoSup.name })}
                            {linkedCount === 1 && <span className="font-mono text-[11px] font-normal text-muted">{t("autoFromDocs")}</span>}
                          </span>
                        ) : (
                          <select value={askSupplier[f.field_key] ?? ""}
                            onChange={(e) => setAskSupplier({ ...askSupplier, [f.field_key]: e.target.value })}
                            className="min-h-[36px] rounded-lg border border-line bg-paper px-2 text-[13px] outline-none focus:border-ink">
                            <option value="">{t("selectSupplier")}</option>
                            {suppliers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                          </select>
                        )}
                        {autoSup && linkedCount > 1 && (
                          <select value={askSupplier[f.field_key] ?? ""}
                            onChange={(e) => setAskSupplier({ ...askSupplier, [f.field_key]: e.target.value })}
                            className="min-h-[36px] rounded-lg border border-line bg-paper px-2 text-[13px] outline-none focus:border-ink">
                            <option value="">{t("change")}</option>
                            {suppliers.filter((s) => s.id !== autoSup.id).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                          </select>
                        )}
                        <button onClick={() => void requestFromSupplier(f.field_key, fieldLabel)}
                          className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-ink px-3 text-[13px] font-semibold text-paper transition-all hover:scale-[1.02] active:scale-[0.97]">
                          <Send size={13} /> {t("request")}
                        </button>
                        <button onClick={() => openGapEmail(f.field_key, fieldLabel, f.state)}
                          className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-line bg-paper px-3 text-[13px] font-semibold transition-all hover:scale-[1.02] hover:border-ink active:scale-[0.97]">
                          <Copy size={13} /> {t("writeEmail")}
                        </button>
                      </div>
                      {f.state === "missing" && (
                        <div className="mt-3 border-t border-amber/20 pt-3">
                          <p className="text-[12px] font-semibold text-amber">{t("orEnterManually")}</p>
                          <div className="mt-1.5 flex flex-wrap items-center gap-2">
                            <input
                              value={manual[f.field_key] ?? ""}
                              onChange={(e) => setManual({ ...manual, [f.field_key]: e.target.value })}
                              placeholder={t("manualPlaceholder")}
                              className="min-h-[36px] w-56 rounded-lg border border-line bg-paper px-2.5 text-[13px] outline-none focus:border-ink"
                            />
                            <button onClick={() => void saveManual(f.field_key, fieldLabel)}
                              disabled={savingManual === f.field_key}
                              className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-moss px-3 text-[13px] font-semibold text-white disabled:opacity-60">
                              <Check size={13} /> {savingManual === f.field_key ? t("saving") : t("saveValue")}
                            </button>
                          </div>
                        </div>
                      )}
                      {lastDraft?.fieldKey === f.field_key && (
                        <div className="mt-2 rounded-lg border border-moss/30 bg-moss-wash p-3 text-[13px]">
                          <p className="font-semibold text-moss-deep">{t("savedFollowup", { label: lastDraft.label, recipient: lastDraft.recipient })}</p>
                          <p className="mt-0.5 text-moss-deep/80">{t("followupWaits")} <button className="font-semibold underline" onClick={() => openGapEmail(f.field_key, fieldLabel, lastDraft.state)}>{t("writeEmailNow")}</button>{t("pasteSend")}</p>
                        </div>
                      )}
                    </div>
                    );
                  })()}

                  {f.values.length === 0 && <p className="mt-3 text-sm italic text-muted">{t("noValues")}</p>}

                  <div className="mt-2 divide-y divide-dashed divide-line">
                    {f.values.map((v) => (
                      <div key={v.id} className="py-3">
                        <p className="text-[15px] font-medium leading-relaxed">
                          {v.value}{v.unit ? <span className="text-muted"> {v.unit}</span> : null}
                        </p>
                        <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-xs text-muted">
                          <span className={clsx("font-semibold", v.status === "accepted" || v.status === "corrected" ? "text-moss-deep" : v.status === "rejected" ? "text-muted line-through" : "text-amber")}>
                            {((t(`valueStatus.${v.status}` as any) || v.status) as string).toUpperCase()}
                          </span>
                          {v.extracted_by === "manual" ? (
                            <span className="inline-flex items-center gap-1"><Pencil size={12} />{t("manualEntry")}</span>
                          ) : v.location ? (
                            <span className="inline-flex items-center gap-1"><FileText size={12} />{v.location}</span>
                          ) : null}
                        </p>
                        {v.status === "proposed" && (
                          <div className="mt-2.5 flex flex-wrap items-center gap-2">
                            <ActionButton onClick={() => void act(`/api/fields/${v.id}/accept`, undefined, t("acceptedMsg", { label: fieldLabel }))} icon={Check} label={t("accept")} tone="border-moss/40 bg-moss-wash text-moss-deep hover:bg-moss hover:text-white" />
                            <ActionButton onClick={() => void act(`/api/fields/${v.id}/reject`, undefined, t("rejectedMsg", { label: fieldLabel }))} icon={X} label={t("reject")} tone="border-line bg-paper text-inksoft hover:border-brick hover:text-brick" />
                            {f.state === "conflicting" && (
                              <ActionButton
                                onClick={() => void act(`/api/products/${params.id}/fields/${f.field_key}/resolve-conflict`, { chosen_field_value_id: v.id }, t("resolvedMsg", { label: fieldLabel }))}
                                icon={Check} label={t("resolveConflict")} tone="border-brick/40 bg-brick-wash text-brick hover:bg-brick hover:text-white"
                              />
                            )}
                            <span className="inline-flex min-h-[36px] items-center gap-2">
                              <input
                                placeholder={t("correctedPlaceholder")}
                                value={edit[v.id] ?? ""}
                                onChange={(e) => setEdit({ ...edit, [v.id]: e.target.value })}
                                className="min-h-[36px] w-44 rounded-lg border border-line bg-paper px-2.5 text-sm outline-none transition-shadow duration-150 focus:border-ink focus:ring-2 focus:ring-ink/15"
                              />
                              <ActionButton
                                onClick={() => edit[v.id]?.trim() ? void act(`/api/fields/${v.id}/correct`, { value: edit[v.id].trim() }, t("correctedMsg", { label: fieldLabel })) : setError(t("typeCorrected"))}
                                icon={Pencil} label={t("editAccept")} tone="border-ink/30 bg-ink text-paper hover:bg-ink/85"
                              />
                            </span>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>

                  {f.state === "verified" && (
                    <div className="mt-3 border-t border-line pt-3">
                      {replaceOpen[f.field_key] ? (
                        <div className="flex flex-wrap items-center gap-2">
                          <input
                            value={replace[f.field_key] ?? verifiedValue}
                            onChange={(e) => setReplace({ ...replace, [f.field_key]: e.target.value })}
                            placeholder={t("replacementPlaceholder")}
                            className="min-h-[36px] w-56 rounded-lg border border-line bg-paper px-2.5 text-[13px] outline-none focus:border-ink"
                          />
                          <ActionButton
                            onClick={() => void replaceVerified(f.field_key, fieldLabel)}
                            icon={Check}
                            label={savingReplace === f.field_key ? t("saving") : t("saveValue")}
                            tone="border-moss/40 bg-moss-wash text-moss-deep hover:bg-moss hover:text-white"
                          />
                          <button
                            onClick={() => { setReplaceOpen({ ...replaceOpen, [f.field_key]: false }); setReplace({ ...replace, [f.field_key]: "" }); }}
                            className="inline-flex min-h-[36px] items-center rounded-lg px-3 text-[13px] font-semibold text-muted hover:text-ink"
                          >
                            {t("cancel")}
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => { setReplaceOpen({ ...replaceOpen, [f.field_key]: true }); setReplace({ ...replace, [f.field_key]: verifiedValue }); }}
                          className="inline-flex min-h-[32px] items-center gap-1.5 rounded-lg px-2 text-xs font-semibold text-muted transition-colors hover:bg-parchment hover:text-ink"
                        >
                          <Pencil size={13} /> {t("editVerified")}
                        </button>
                      )}
                    </div>
                  )}
                </section>
              );
            })}
          </div>

          {(detail.documents ?? []).length > 0 && (
            <div className="mt-6 rounded-xl2 border border-line bg-white/70 p-5 shadow-card">
              <h2 className="font-display text-lg font-semibold">{t("sourceDocs")}</h2>
              <p className="mt-1 text-[13px] text-muted">{t("sourceDocsHint")}</p>
              <div className="mt-3 space-y-2">
                {(detail.documents ?? []).map((d: any) => {
                  const docStatus = (t(`docStatus.${d.extraction_status}` as any) || d.extraction_status) as string;
                  const items = d.items_count ?? 0;
                  const done = d.extraction_status === "done";
                  const summary = (
                    <>
                      {d.filename}
                      <span className="ml-2 font-mono text-xs font-normal text-muted">
                        {docStatus}{typeof d.extracted_count === "number" ? ` · ${t("passportValues", { count: d.extracted_count })}` : ""}
                      </span>
                      {done && (
                        <span className="ml-2 text-xs font-normal text-muted">
                          {items > 0 ? t("allExtracted") : t("passportOnly")}
                        </span>
                      )}
                    </>
                  );
                  const meta = (
                    <p className="mt-1 font-mono text-[11px] text-muted">
                      {d.document_type || t("typeUnknown")}
                      {typeof d.page_count === "number" ? ` · ${t("pages", { count: d.page_count })}` : ""}
                      {typeof d.file_size === "number" ? ` · ${(d.file_size / (1024 * 1024)).toFixed(1)} MB` : ""}
                      {d.is_scanned ? ` · ${t("scanned")}` : ""}
                      {d.document_language ? ` · ${d.document_language}` : ""}
                    </p>
                  );
                  const retry = d.extraction_status === "failed" && (
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <p className="text-[13px] text-brick">{d.extraction_error || t("extractionFailed")}</p>
                      <button onClick={() => void retryDocument(d.id)} disabled={retrying === d.id}
                        className="inline-flex min-h-[32px] items-center gap-1.5 rounded-lg border border-brick/30 bg-brick-wash px-2.5 text-xs font-semibold text-brick disabled:opacity-60">
                        {retrying === d.id ? t("retrying") : t("retryExtraction")}
                      </button>
                    </div>
                  );
                  if (done && items === 0) {
                    return (
                      <div key={d.id} className="rounded-xl border border-line bg-paper px-4 py-2.5 text-sm font-semibold">
                        {summary}
                        {meta}
                      </div>
                    );
                  }
                  return (
                    <details key={d.id} className="rounded-xl border border-line bg-paper px-4 py-2.5">
                      <summary className="cursor-pointer text-sm font-semibold">{summary}</summary>
                      {meta}
                      {d.document_summary && <p className="mt-1 text-[13px] text-inksoft">{d.document_summary}</p>}
                      {retry}
                      {(d.extraction_status === "pending" || d.extraction_status === "extracting") && (
                        <p className="mt-3 text-[13px] text-muted">{t("extractionRunning")}</p>
                      )}
                      {done && items > 0 && <DocumentItems documentId={d.id} />}
                    </details>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        <aside className="lg:sticky lg:top-24 lg:self-start">
          <div className="rounded-xl2 border border-line bg-white/70 p-5 shadow-card">
            <div className="flex justify-center"><ReadinessRing percent={detail.readiness_percent} /></div>
            <div className="ledger-rule my-4" />
            <dl className="space-y-2 text-sm">
              {COUNT_STYLE.map(([k, dot]) => (
                <div key={k} className="flex items-center gap-2.5">
                  <span className={clsx("h-2.5 w-2.5 rounded-full", dot)} />
                  <dt className="capitalize text-inksoft">{t(`counts.${k}` as any)}</dt>
                  <dd className="ml-auto font-semibold tabular-nums">{counts[k] ?? 0}</dd>
                </div>
              ))}
              <div className="flex items-center gap-2.5 border-t border-line pt-2">
                <dt className="font-semibold">{t("actionsRequired")}</dt>
                <dd className="ml-auto font-display text-xl font-semibold tabular-nums">{detail.actions_required}</dd>
              </div>
            </dl>
          </div>

          <div className="mt-4">
            <UploadDropzone onFiles={(fs) => void uploadFiles(fs)} busy={uploading} />
          </div>

          <div className="mt-4 rounded-xl2 border border-line bg-white/70 p-5 shadow-card">
            {published ? (
              <a href={`/passport/${product.public_slug}`} className="flex min-h-[44px] items-center justify-center gap-2 rounded-xl bg-moss font-semibold text-white transition-transform duration-150 hover:scale-[1.01] active:scale-[0.99]">
                {t("viewPublic")} <ExternalLink size={16} />
              </a>
            ) : (
              <button
                onClick={() => void act(`/api/products/${params.id}/publish`, undefined, t("published"))}
                className="flex min-h-[44px] w-full items-center justify-center gap-2 rounded-xl bg-ink font-semibold text-paper transition-transform duration-150 hover:scale-[1.01] active:scale-[0.99]"
              >
                <Rocket size={16} /> {missingFields.length > 0 ? t("publishBlockedCount", { count: missingFields.length }) : t("publishPassport")}
              </button>
            )}
            <p className="mt-2.5 text-xs leading-relaxed text-muted">{t("publishNote", { count: detail.total_required })}</p>
          </div>
        </aside>
      </div>
      {mailDraft && (
        <EmailDraftModal
          initialSubject={mailDraft.subject}
          initialBody={mailDraft.body}
          hasSignature={mailDraft.hasSignature}
          onClose={() => setMailDraft(null)}
        />
      )}
    </>
  );
}
