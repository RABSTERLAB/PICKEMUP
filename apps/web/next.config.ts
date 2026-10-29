import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  // dev on this machine is addressed both as localhost and 127.0.0.1; without this
  // Next 16 blocks dev client resources from the "wrong" origin and the app never hydrates
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
