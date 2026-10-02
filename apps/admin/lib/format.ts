export function usd(value: string | number | undefined | null): string {
  const n = typeof value === "string" ? Number(value) : (value ?? 0);
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(n);
}

export function pct(value: number | undefined | null, digits = 1): string {
  return `${(value ?? 0).toFixed(digits)}%`;
}

export function date(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "-";
}
