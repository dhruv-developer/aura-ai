"use client";

import { xrayPublic, type XRayReport } from "@aura/api-client";
import { useState } from "react";
import { ErrorBox, errorMessage, pct, Stat, usd } from "@/components/ui";
import { XRayReportView } from "@/components/xray-report";

export default function PublicXRay() {
  const [report, setReport] = useState<XRayReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(form: FormData) {
    setBusy(true);
    setError(null);
    try {
      setReport((await xrayPublic.submit(form)).report);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="py-16">
      <h1 className="text-4xl font-bold tracking-tight">Get your AI Workload Report</h1>
      <p className="mt-3 max-w-2xl text-zinc-600">
        Upload a sample of your LLM request logs (OpenAI-style JSONL or CSV). We return spend by
        workload, waste and projected savings. We keep only the headline numbers, never your prompts.
      </p>
      <form action={submit} className="card mt-8 grid max-w-xl gap-4">
        <div>
          <label className="label" htmlFor="email">Work email</label>
          <input className="input" id="email" name="email" type="email" required />
        </div>
        <div>
          <label className="label" htmlFor="company">Company</label>
          <input className="input" id="company" name="company" />
        </div>
        <div>
          <label className="label" htmlFor="format">Format</label>
          <select className="input" id="format" name="format" defaultValue="jsonl">
            <option value="jsonl">JSONL (OpenAI request/response logs)</option>
            <option value="csv">CSV (model, input_tokens, output_tokens, …)</option>
          </select>
        </div>
        <div>
          <label className="label" htmlFor="file">Log sample (max 10 MB)</label>
          <input className="input" id="file" name="file" type="file" required />
        </div>
        <ErrorBox error={error} />
        <button className="btn-primary" disabled={busy}>{busy ? "Analysing…" : "Generate report"}</button>
      </form>
      {report ? (
        <div className="mt-12">
          <div className="grid gap-4 md:grid-cols-4">
            <Stat label="Monthly AI spend" value={usd(report.summary.monthly_spend_usd)} />
            <Stat label="Expected savings / month" value={usd(report.summary.est_monthly_savings_usd)} />
            <Stat label="Savings" value={pct(report.summary.est_savings_pct)} />
            <Stat label="Requests analysed" value={report.summary.requests.toLocaleString()} />
          </div>
          <XRayReportView report={report} />
        </div>
      ) : null}
    </section>
  );
}
