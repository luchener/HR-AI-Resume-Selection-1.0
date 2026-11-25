import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_INTERNAL_URL ?? 'http://127.0.0.1:9001';

const nextConfig: NextConfig = {
  serverExternalPackages: ['playwright-core'],
  images: {
    // The app only serves local SVG brand assets. Disabling the optimizer
    // removes the remote image fetch/transform attack surface entirely.
    unoptimized: true,
  },
  experimental: {
    // Batch AI analysis is serialized and can legitimately exceed Next's
    // 30-second rewrite proxy default. Keep this aligned with deployment timeouts.
    proxyTimeout: 1_200_000,
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
