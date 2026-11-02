import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  experimental: {
    // Multiple static-generation workers race to create segment directories on Windows.
    cpus: 1,
  },
};

export default nextConfig;
