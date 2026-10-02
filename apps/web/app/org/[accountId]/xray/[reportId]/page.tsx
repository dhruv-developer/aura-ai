"use client";

import { accounts } from "@aura/api-client";
import { useParams } from "next/navigation";
import { ErrorBox, Loading, PageTitle, Stat, useAsync } from "@/components/ui";
import { XRayReportView } from "@/components/xray-report";
import { pct, usd } from "@/lib/format";

export default function XRayDetail() {
  const { accountId, reportId } = useParams<{ accountId: string; reportId: string }>();
  const r = useAsync(() => accounts.xray(accountId, reportId), [accountId, reportId]);
  const report = r.data?.report;
  return (
    <>
      <PageTitle title={r.data?.name ?? "X-Ray"}>
        <a className="btn-secondary" href={`/v1/accounts/${accountId}/xray/${reportId}/report.html`} target="_blank" rel="noreferrer">
          Printable report
        </a>
      </PageTitle>
      <ErrorBox error={r.error} />
      {r.loading ? <Loading /> : null}
      {report ? (
        <>
          <div className="grid gap-4 md:grid-cols-4">
            <Stat label="Monthly AI spend" value={usd(report.summary.monthly_spend_usd)} />
            <Stat label="Expected savings / month" value={usd(report.summary.est_monthly_savings_usd)} hint={`potential ${usd(report.summary.potential_monthly_savings_usd)}`} />
            <Stat label="Savings" value={pct(report.summary.est_savings_pct)} />
            <Stat label="Projected spend / month" value={usd(report.summary.projected_monthly_spend_usd)} />
          </div>
          <XRayReportView report={report} />
        </>
      ) : null}
    </>
  );
}
