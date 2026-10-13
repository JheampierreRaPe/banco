import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError, friendlyErrorMessage } from "../api/client";
import { formatDtiPercent, formatPEN, maskRef, shortRef } from "../domain/masking";
import type { LoanApplication, ReviewAudit, ReviewDecision } from "../domain/types";

interface DetailProps {
  app: LoanApplication;
  productLabel: string | null;
  onReviewed: (audit: ReviewAudit, decided: LoanApplication) => void;
  onConflict: (applicationId: string) => void;
  review: (decision: ReviewDecision, reason: string) => Promise<LoanApplication>;
  reviewing: boolean;
}

// Detalle del expediente (construido con la fila de la bandeja —ver gap G52—)
// + dictamen manual con motivo obligatorio (HU10 CA-03).
export function ApplicationDetail({ app, productLabel, onReviewed, onConflict, review, reviewing }: DetailProps) {
  const [reason, setReason] = useState("");
  const [touched, setTouched] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const ref = shortRef(app.id);
  const reasonMissing = reason.trim().length === 0;

  const submit = async (decision: ReviewDecision) => {
    setTouched(true);
    // Sin motivo no se envía (el servidor también valida: 422).
    if (reasonMissing || reviewing) return;
    setServerError(null);
    try {
      const decided = await review(decision, reason.trim());
      onReviewed(
        {
          applicationShortRef: ref,
          actorShortRef: "analista",
          at: new Date().toISOString(),
          before: "IN_REVIEW",
          after: decision,
        },
        decided,
      );
      setReason("");
      setTouched(false);
    } catch (err) {
      // 409: ya decidido -> sale de la bandeja y se avisa.
      if (err instanceof ApiError && (err.code === "CONFLICT" || err.status === 409)) {
        onConflict(app.id);
      }
      setServerError(friendlyErrorMessage(err));
    }
  };

  const onSubmitApprove = (e: FormEvent) => {
    e.preventDefault();
    void submit("APPROVED");
  };
  const onSubmitReject = (e: FormEvent) => {
    e.preventDefault();
    void submit("REJECTED");
  };

  const bureauLine =
    app.bureau_authorized && app.bureau_authorized_at
      ? `Consentimiento buró: SÍ - ${app.bureau_authorized_at} (auditado)`
      : "Consentimiento buró: pendiente de registro";

  return (
    <section aria-label={`Detalle ${ref}`}>
      <h1>
        Detalle {ref} (enmascarado)
      </h1>
      <p className="subtitle">Expediente en revisión manual · datos del servidor</p>

      <div className="card">
        <p className="card-label">SOLICITUD</p>
        <h3>
          {productLabel ?? `Producto ${maskRef(app.product_id)}`} - {formatPEN(app.requested_amount_minor)} -{" "}
          {app.term_months} m
        </h3>
        <p className="card-value">Cuenta {maskRef(app.user_id)}</p>
        <p className="card-value">Solicitante {maskRef(app.user_id)}</p>
      </div>

      <div className="card">
        <p className="card-label">RIESGO (servidor)</p>
        <p className="card-value">
          Score {app.score ?? "—"} - {formatDtiPercent(app.dti_ratio)} - Buró:{" "}
          {app.score === null ? "pendiente" : "evaluado"}
        </p>
        {app.decision_reason && <p className="card-value">Motivo previo: {app.decision_reason}</p>}
      </div>

      <div className="banner-consent" role="note">
        {bureauLine}
      </div>

      <div className="card review-box">
        <form>
          <label htmlFor={`reason-${app.id}`}>Dictamen manual (motivo obligatorio *)</label>
          <textarea
            id={`reason-${app.id}`}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Verificado sustento de ingresos. DTI dentro de política…"
            aria-label="Motivo del dictamen"
          />
          {touched && reasonMissing && (
            <p className="field-error" role="alert">
              Sin motivo no se puede enviar (el servidor valida).
            </p>
          )}
          {serverError && (
            <p className="field-error" role="alert">
              {serverError}
            </p>
          )}
          <div className="btn-row">
            <button
              type="submit"
              className="btn btn-approve"
              disabled={reviewing}
              onClick={onSubmitApprove}
            >
              {reviewing ? "Enviando…" : "Aprobar"}
            </button>
            <button
              type="submit"
              className="btn btn-reject"
              disabled={reviewing}
              onClick={onSubmitReject}
            >
              {reviewing ? "Enviando…" : "Rechazar"}
            </button>
          </div>
        </form>
      </div>

      <p className="notice-mask">
        Tras dictaminar sale de la bandeja y queda auditado (usuario, hora, antes/después). 409 si
        ya fue decidido.
      </p>
    </section>
  );
}
