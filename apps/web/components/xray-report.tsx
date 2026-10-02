import type { XRayReport } from "@aura/api-client";
import { usd } from "@/lib/format";

export function XRayReportView({ report }: { report: XRayReport }) {
  return (
    <div className="mt-8 space-y-10">
      <div>
        <h2 className="mb-3 text-lg font-semibold">Optimization opportunities</h2>
        <table className="table">
          <thead>
            <tr><th>Opportunity</th><th>Workload</th><th>Requests</th><th>Expected $/mo</th><th>Confidence</th></tr>
          </thead>
          <tbody>
            {report.opportunities.map((o) => (
              <tr key={`${o.kind}-${o.workload}`} title={o.recommendation}>
                <td>{o.title}</td>
                <td>{o.workload}</td>
                <td>{o.affected_requests.toLocaleString()}</td>
                <td>{usd(o.expected_monthly_savings_usd)}</td>
                <td>{Math.round(o.confidence * 100)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        <h2 className="mb-3 text-lg font-semibold">Workloads</h2>
        <table className="table">
          <thead>
            <tr><th>Workload</th><th>Requests</th><th>Spend $/mo</th><th>Expected savings</th><th>Models</th></tr>
          </thead>
          <tbody>
            {report.workloads.map((w) => (
              <tr key={w.key}>
                <td>{w.label}</td>
                <td>{w.requests.toLocaleString()}</td>
                <td>{usd(w.monthly_spend_usd)}</td>
                <td>{usd(w.est_monthly_savings_usd)}</td>
                <td className="text-xs text-zinc-500">{w.models.join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-zinc-500">{report.summary.methodology}</p>
    </div>
  );
}
