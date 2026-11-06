/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // AWS deployment: the page is fully client-side, so it is exported as
  // static files and served from S3 + CloudFront (no server to pay for).
  output: "export",
  images: { unoptimized: true },
};

module.exports = nextConfig;
