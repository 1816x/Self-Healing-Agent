import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `node:sqlite` is a Node builtin, so it must never be bundled or polyfilled
  // for the browser. Every page that reads the store is a server component and
  // declares `runtime = "nodejs"`; this is the belt to that's braces.
  serverExternalPackages: ["node:sqlite"],
};

export default nextConfig;
