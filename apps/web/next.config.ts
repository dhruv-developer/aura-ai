import type { NextConfig } from "next";

const API = process.env.AURA_API_URL ?? "http://localhost:8000";
const RUNTIME = process.env.AURA_RUNTIME_URL ?? "http://localhost:8100";

const config: NextConfig = {
  reactStrictMode: true,
  // Gzip buffers proxied server-sent events, which would make streamed answers arrive
  // all at once. Compression belongs at the CDN / load balancer in production anyway.
  compress: false,
  // The dev-only badge covers app controls (theme switches) in every corner.
  devIndicators: false,
  transpilePackages: ["@aura/api-client"],
  // Same-origin proxy: the browser talks to /v1/* on this origin, so the HttpOnly
  // session cookie is first-party and no CORS configuration is needed.
  async rewrites() {
    return [
      { source: "/v1/:path*", destination: `${API}/v1/:path*` },
      // The personal app talks to the data plane with the same session cookie.
      { source: "/rt/v1/:path*", destination: `${RUNTIME}/v1/:path*` },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "SAMEORIGIN" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default config;
