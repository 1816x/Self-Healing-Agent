import next from "eslint-config-next";
import nextTypeScript from "eslint-config-next/typescript";
import nextWebVitals from "eslint-config-next/core-web-vitals";

const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...next,
  ...nextTypeScript,
  ...nextWebVitals,
  {
    // App Router only — there is no `pages/` directory for this rule to check.
    rules: { "@next/next/no-html-link-for-pages": "off" },
  },
];

export default config;
