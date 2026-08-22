/** @type {import("next").NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The browser must never hold broker credentials. Only NEXT_PUBLIC_* values
  // reach the client bundle, and the only one defined is the API base URL.
  env: {
    NEXT_PUBLIC_API_BASE_URL:
      process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
  },
};

export default nextConfig;
