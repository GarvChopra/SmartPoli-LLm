import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";
import { chromium } from "../../launch-video/engine/node_modules/playwright-core/index.mjs";

const DEMO_DIR = path.resolve("launches/smartpoli-demo");
const OUT_DIR = path.join(DEMO_DIR, "out");
const TIMELINE_PATH = path.join(DEMO_DIR, "timeline.json");
const PLAYER_PATH = path.join(DEMO_DIR, "dt_player.html");
const FFMPEG = "C:\\Users\\shash\\AppData\\Local\\Programs\\Python\\Python39\\lib\\site-packages\\imageio_ffmpeg\\binaries\\ffmpeg-win-x86_64-v7.1.exe";

const BROWSER_CANDIDATES = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
];

function findBrowser() {
  for (const p of BROWSER_CANDIDATES) {
    if (fs.existsSync(p)) return p;
  }
  throw new Error("No Chromium/Edge binary found.");
}

function runFfmpeg(args) {
  return new Promise((resolve, reject) => {
    const p = spawn(FFMPEG, args, { stdio: ["ignore", "pipe", "pipe"] });
    let err = "";
    p.stderr.on("data", (d) => (err += d.toString()));
    p.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`ffmpeg exited ${code}: ${err.slice(-600)}`));
    });
  });
}

async function renderBoard(browser, playerUrl, timeline) {
  const times = [
    { t: 0.60,  tag: "0.6s Intro" },
    { t: 1.95,  tag: "1.95s Hovering Avatar" },
    { t: 3.60,  tag: "3.6s Companion Open" },
    { t: 8.80,  tag: "8.8s Touch & Talk KB" },
    { t: 13.80, tag: "13.8s Call Trigger+Stats" },
    { t: 16.75, tag: "16.75s Phone Link Incoming" },
    { t: 24.50, tag: "24.5s Active BP Call" },
    { t: 36.50, tag: "36.5s Urgent #SP-1042" },
    { t: 43.20, tag: "43.2s WhatsApp BP" },
    { t: 46.80, tag: "46.8s WA Ticket Link" },
    { t: 50.20, tag: "50.2s Ticket Portal" },
    { t: 55.50, tag: "55.5s Outro Card" },
  ];

  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  await page.goto(playerUrl, { waitUntil: "load" });
  await page.evaluate(async (tl) => await window.__load({ timeline: tl }), timeline);

  const tmpFrames = [];
  for (let i = 0; i < times.length; i++) {
    await page.evaluate((t) => window.__seek(t), times[i].t);
    const outJpg = path.join(OUT_DIR, `.board_${i}.jpg`);
    await page.screenshot({ path: outJpg, type: "jpeg", quality: 92 });
    tmpFrames.push(outJpg);
  }
  await page.close();

  const pattern = path.join(OUT_DIR, ".board_%d.jpg");
  const boardOut = path.join(OUT_DIR, "board_dt.jpg");
  await runFfmpeg([
    "-y", "-i", pattern,
    "-vf", "scale=640:360,tile=4x3:padding=8:color=0b1815",
    "-q:v", "3",
    boardOut,
  ]);
  for (const f of tmpFrames) fs.rmSync(f, { force: true });
  console.log(`Board written -> ${boardOut}`);
}

async function renderSegmentWorker(browser, playerUrl, timeline, workerId, startF, endF, fps, segOut) {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  await page.goto(playerUrl, { waitUntil: "load" });
  await page.evaluate(async (tl) => await window.__load({ timeline: tl }), timeline);

  const ff = spawn(FFMPEG, [
    "-y", "-f", "image2pipe", "-vcodec", "mjpeg", "-r", String(fps), "-i", "-",
    "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
    segOut,
  ], { stdio: ["pipe", "ignore", "pipe"] });

  let ffErr = "";
  ff.stderr.on("data", (d) => (ffErr += d.toString()));

  const ffDone = new Promise((res, rej) => {
    ff.on("close", (code) => (code === 0 ? res() : rej(new Error(`seg${workerId} ffmpeg ${code}: ${ffErr.slice(-400)}`))));
  });

  for (let f = startF; f < endF; f++) {
    const t = f / fps;
    await page.evaluate((sec) => window.__seek(sec), t);
    const buf = await page.screenshot({ type: "jpeg", quality: 94 });
    if (!ff.stdin.write(buf)) {
      await new Promise((r) => ff.stdin.once("drain", r));
    }
    if ((f - startF) % 90 === 0) {
      console.log(`[worker ${workerId}] frame ${f}/${endF}`);
    }
  }
  ff.stdin.end();
  await ffDone;
  await page.close();
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const args = new Set(process.argv.slice(2));
  const isBoard = args.has("--board");
  const timeline = JSON.parse(fs.readFileSync(TIMELINE_PATH, "utf8"));
  const fps = 30;
  const totalFrames = Math.ceil(timeline.duration * fps);
  const playerUrl = pathToFileURL(PLAYER_PATH).href;

  const browser = await chromium.launch({
    executablePath: findBrowser(),
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-gpu-sandbox",
      "--autoplay-policy=no-user-gesture-required",
      "--use-gl=angle",
      "--use-angle=swiftshader",
      "--enable-webgl",
      "--ignore-gpu-blocklist",
    ],
  });

  try {
    if (isBoard) {
      await renderBoard(browser, playerUrl, timeline);
      return;
    }

    const t0 = Date.now();
    const workers = 4;
    const per = Math.ceil(totalFrames / workers);
    const segPaths = [];
    const jobs = [];
    for (let w = 0; w < workers; w++) {
      const s = w * per;
      const e = Math.min(totalFrames, s + per);
      if (s >= e) break;
      const segPath = path.join(OUT_DIR, `.dt_seg${w}.mp4`);
      segPaths.push(segPath);
      jobs.push(renderSegmentWorker(browser, playerUrl, timeline, w, s, e, fps, segPath));
    }
    await Promise.all(jobs);

    const concatTxt = path.join(OUT_DIR, ".dt_concat.txt");
    fs.writeFileSync(concatTxt, segPaths.map((p) => `file '${p.replace(/\\/g, "/")}'`).join("\n"));

    const silentMp4 = path.join(OUT_DIR, ".dt_silent.mp4");
    await runFfmpeg(["-y", "-f", "concat", "-safe", "0", "-i", concatTxt, "-c", "copy", silentMp4]);

    const mixWav = path.join(DEMO_DIR, timeline.mix || "out/mix.wav");
    const finalMp4 = path.join(OUT_DIR, "smartpoli-demo.mp4");
    await runFfmpeg([
      "-y", "-i", silentMp4, "-i", mixWav,
      "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
      "-shortest", "-movflags", "+faststart",
      finalMp4,
    ]);

    const reviewMp4 = path.join(OUT_DIR, "smartpoli-demo-review.mp4");
    await runFfmpeg([
      "-y", "-i", finalMp4,
      "-vf", "scale=1280:720",
      "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
      "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
      reviewMp4,
    ]);

    for (const p of [...segPaths, concatTxt, silentMp4]) fs.rmSync(p, { force: true });
    const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    console.log(`Rendered ${totalFrames} frames in ${elapsed}s -> ${finalMp4} + ${reviewMp4}`);
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
