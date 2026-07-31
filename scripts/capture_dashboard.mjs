/**
 * Screenshots the dashboard for the README, from a real running server.
 *
 * Nothing here is a mockup: it drives a real Chromium against a real Next
 * server reading a real SQLite store. Run the server first (the seed script
 * fills a database with one incident per pipeline outcome, including the arms
 * a demo run rarely produces):
 *
 *   ./monitor/bin/monitor --db demo.db --log-file /dev/null   # ctrl-c once created
 *   python scripts/seed_dashboard_db.py demo.db
 *   cd dashboard && INCIDENTS_DB=../demo.db npm run build && \
 *     INCIDENTS_DB=../demo.db npx next start -p 3111 &
 *   node scripts/capture_dashboard.mjs http://127.0.0.1:3111 docs/assets
 */
import { mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { pathToFileURL } from "node:url";

// Playwright is a screenshot tool, not a dependency of anything that ships, so
// it is deliberately absent from every package.json in this repo. Resolve it
// from wherever it happens to be installed — locally or globally — rather than
// forcing `npm ci` to pull a browser driver into CI.
const { chromium } = await importPlaywright();

async function importPlaywright() {
  try {
    return await import("playwright");
  } catch {
    // Not resolvable from here; try a global install.
  }
  try {
    const root = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
    return await import(pathToFileURL(path.join(root, "playwright", "index.mjs")).href);
  } catch {
    throw new Error(
      "playwright is not installed. Run `npm i -g playwright` (Chromium is already " +
        "present if PLAYWRIGHT_BROWSERS_PATH is set), then re-run this script.",
    );
  }
}

const base = process.argv[2] ?? "http://127.0.0.1:3111";
const outDir = process.argv[3] ?? "docs/assets";

const SHOTS = [
  { file: "dashboard-list.png", route: "/", height: 1180 },
  { file: "dashboard-detail.png", route: "/incidents/2", height: 1600 },
];

await mkdir(outDir, { recursive: true });

const browser = await chromium.launch();
try {
  for (const shot of SHOTS) {
    const page = await browser.newPage({
      viewport: { width: 1180, height: shot.height },
      deviceScaleFactor: 2,
      colorScheme: "dark",
    });
    const response = await page.goto(base + shot.route, { waitUntil: "networkidle" });
    if (!response || !response.ok()) {
      throw new Error(`${shot.route} returned ${response ? response.status() : "no response"}`);
    }
    const target = path.join(outDir, shot.file);
    await page.screenshot({ path: target, fullPage: true });
    console.log(`wrote ${target}`);
    await page.close();
  }
} finally {
  await browser.close();
}
