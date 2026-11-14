#!/usr/bin/env node
/**
 * Digital-Twin Screen & WhatsApp UI Capture Script for the SmartPoli Care Navigator demo video.
 * Captures 9 crisp 1600x960 @ 2x (3200x1920) PNGs into launches/smartpoli-demo/assets/dt/
 * and prints normalized (0..1) coordinates of interactive UI targets.
 *
 * Safety:
 * - Never reads .env
 * - Never clicks #callMyPhoneBtn or calls /voice/call/outbound
 * - Only uses in-browser chat (#chatForm), simulated phone tab (#phoneTabBtn / #dialCallBtn / #phoneForm),
 *   local whatsapp_mock.html, and GET /ticket/SP-1042.
 */
import { chromium } from "../../launch-video/engine/node_modules/playwright-core/index.mjs";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DT_DIR = path.join(HERE, "assets", "dt");
fs.mkdirSync(DT_DIR, { recursive: true });

const BASE_URL = process.env.SMARTPOLI_URL || "http://127.0.0.1:8100";
const EDGE_PATH = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const VIEWPORT = { width: 1600, height: 960 };

if (!fs.existsSync(EDGE_PATH)) {
  throw new Error(`Microsoft Edge not found at ${EDGE_PATH}`);
}

const coordsReport = {};

async function measureCenter(page, selector) {
  return await page.evaluate(
    ({ sel, vw, vh }) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || el.hidden) return null;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return null;
      return {
        x: +((r.left + r.width / 2) / vw).toFixed(4),
        y: +((r.top + r.height / 2) / vh).toFixed(4),
        box: {
          x: +(r.left / vw).toFixed(4),
          y: +(r.top / vh).toFixed(4),
          w: +(r.width / vw).toFixed(4),
          h: +(r.height / vh).toFixed(4),
        },
        px: {
          left: Math.round(r.left),
          top: Math.round(r.top),
          width: Math.round(r.width),
          height: Math.round(r.height),
        },
      };
    },
    { sel: selector, vw: VIEWPORT.width, vh: VIEWPORT.height }
  );
}

async function handleFloatingDock(page, hideCompletely = false) {
  await page.evaluate((hideAll) => {
    const dock = document.getElementById("floatingMascotDock");
    if (!dock) return;
    if (hideAll) {
      dock.style.display = "none";
      return;
    }
    dock.style.display = "";
    const dRect = dock.getBoundingClientRect();
    const main = document.querySelector("main");
    if (!main) return;
    const mRect = main.getBoundingClientRect();
    const overlaps = !(
      dRect.right < mRect.left - 12 ||
      dRect.left > mRect.right + 12 ||
      dRect.bottom < mRect.top - 12 ||
      dRect.top > mRect.bottom + 12
    );
    if (overlaps) {
      dock.style.display = "none";
    }
  }, hideCompletely);
}

async function sendChatAndWait(page, text, timeoutMs = 45000) {
  const beforeBots = await page.locator("#log .msg.bot").count();
  await page.fill("#msgInput", text);
  await page.click("#sendBtn");

  await page.waitForFunction(
    (prevCount) => {
      const bots = document.querySelectorAll("#log .msg.bot").length;
      const btn = document.getElementById("sendBtn");
      const chip = document.getElementById("statusChip");
      const chipText = (chip?.textContent || "").trim().toLowerCase();
      return bots > prevCount && btn && !btn.disabled && chipText !== "thinking";
    },
    beforeBots,
    { timeout: timeoutMs }
  );

  await page.evaluate(() => {
    const log = document.getElementById("log");
    if (log) log.scrollTop = log.scrollHeight;
  });
  await page.waitForTimeout(450);
}

async function sendPhoneTurnAndWait(page, text, timeoutMs = 45000) {
  const beforeBots = await page.locator("#phoneLog .msg.bot").count();
  await page.fill("#phoneInput", text);
  await page.click("#phoneSendBtn");

  await page.waitForFunction(
    (prevCount) => {
      const bots = document.querySelectorAll("#phoneLog .msg.bot").length;
      const badge = document.getElementById("phoneStatusBadge");
      const badgeText = (badge?.textContent || "").trim().toLowerCase();
      return bots > prevCount && badgeText !== "processing...";
    },
    beforeBots,
    { timeout: timeoutMs }
  );

  await page.evaluate(() => {
    const phoneLog = document.getElementById("phoneLog");
    if (phoneLog) phoneLog.scrollTop = phoneLog.scrollHeight;
  });
  await page.waitForTimeout(350);
}

const forceAll = process.argv.includes("--all");
const needPortal = forceAll || !fs.existsSync(path.join(DT_DIR, "dt_ticket_portal.png"));

const browser = await chromium.launch({
  executablePath: EDGE_PATH,
  headless: true,
});

try {
  // ==================================================================
  // Captures 0–3: Hovering Desktop Avatar & Floating Companion (1600x960 @ 2x)
  // ==================================================================
  const compCtx = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    colorScheme: "dark",
  });
  const compPage = await compCtx.newPage();
  const compFileUrl = pathToFileURL(path.join(DT_DIR, "hover_companion_mock.html")).href;

  // 0) dt_hover_0_avatar.png — Hovering robot avatar with ! badge on coastal desktop
  await compPage.goto(`${compFileUrl}?step=0`, { waitUntil: "load" });
  await compPage.waitForTimeout(250);
  coordsReport["dt_hover_0_avatar.png"] = {
    hoverAvatarBtn: await measureCenter(compPage, "#hoverAvatarBtn"),
  };
  await compPage.screenshot({ path: path.join(DT_DIR, "dt_hover_0_avatar.png") });
  console.log("Captured dt_hover_0_avatar.png");

  // 1) dt_chat_welcome.png — Touched avatar -> floating companion welcome & due med
  await compPage.goto(`${compFileUrl}?step=1`, { waitUntil: "load" });
  await compPage.waitForTimeout(250);
  coordsReport["dt_chat_welcome.png"] = {
    micBtn: await measureCenter(compPage, "#micBtn"),
    sendBtn: await measureCenter(compPage, "#sendBtn"),
    companionWindow: await measureCenter(compPage, "#companionWindow"),
    unwellChip: await measureCenter(compPage, "#unwellChip"),
  };
  await compPage.screenshot({ path: path.join(DT_DIR, "dt_chat_welcome.png") });
  console.log("Captured dt_chat_welcome.png");

  // 2) dt_chat_kb.png — Talk inside hovering companion: grounded KB & abstention
  await compPage.goto(`${compFileUrl}?step=2`, { waitUntil: "load" });
  await compPage.waitForTimeout(250);
  coordsReport["dt_chat_kb.png"] = {
    micBtn: await measureCenter(compPage, "#micBtn"),
    sendBtn: await measureCenter(compPage, "#sendBtn"),
    lastBotMsg: await measureCenter(compPage, "#lastBotMsg"),
    unwellChip: await measureCenter(compPage, "#unwellChip"),
  };
  await compPage.screenshot({ path: path.join(DT_DIR, "dt_chat_kb.png") });
  console.log("Captured dt_chat_kb.png");

  // 3) dt_chat_workflow.png — Tap Unwell -> outbound call initiated to Phone Link
  await compPage.goto(`${compFileUrl}?step=3`, { waitUntil: "load" });
  await compPage.waitForTimeout(250);
  coordsReport["dt_chat_workflow.png"] = {
    unwellChip: await measureCenter(compPage, "#unwellChip"),
    callTriggerCard: await measureCenter(compPage, "#callTriggerCard"),
    companionWindow: await measureCenter(compPage, "#companionWindow"),
  };
  await compPage.screenshot({ path: path.join(DT_DIR, "dt_chat_workflow.png") });
  console.log("Captured dt_chat_workflow.png");

  await compCtx.close();

  // ==================================================================
  // Captures 4–6: Windows 11 Phone Link Mockup (1600x960 @ 2x)
  // ==================================================================
  const plCtx = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    colorScheme: "dark",
  });
  const plPage = await plCtx.newPage();
  const plFileUrl = pathToFileURL(path.join(DT_DIR, "phonelink_mock.html")).href;

  // 4) dt_voice_1_dial.png
  await plPage.goto(`${plFileUrl}?step=1`, { waitUntil: "load" });
  await plPage.waitForTimeout(250);
  coordsReport["dt_voice_1_dial.png"] = {
    dialCallBtn: await measureCenter(plPage, "#dialCallBtn"),
    hangupCallBtn: await measureCenter(plPage, "#hangupCallBtn"),
    phoneStatusBadge: await measureCenter(plPage, "#phoneStatusBadge"),
  };
  await plPage.screenshot({ path: path.join(DT_DIR, "dt_voice_1_dial.png") });
  console.log("Captured dt_voice_1_dial.png");

  // 5) dt_voice_2_bp.png
  await plPage.goto(`${plFileUrl}?step=2`, { waitUntil: "load" });
  await plPage.waitForTimeout(250);
  coordsReport["dt_voice_2_bp.png"] = {
    phoneStatusBadge: await measureCenter(plPage, "#phoneStatusBadge"),
    hangupCallBtn: await measureCenter(plPage, "#hangupCallBtn"),
  };
  await plPage.screenshot({ path: path.join(DT_DIR, "dt_voice_2_bp.png") });
  console.log("Captured dt_voice_2_bp.png");

  // 6) dt_voice_3_urgent.png
  await plPage.goto(`${plFileUrl}?step=3`, { waitUntil: "load" });
  await plPage.waitForTimeout(250);
  coordsReport["dt_voice_3_urgent.png"] = {
    phoneStatusBadge: await measureCenter(plPage, "#phoneStatusBadge"),
    urgentCard: await measureCenter(plPage, ".msg.bot.emergency"),
    hangupCallBtn: await measureCenter(plPage, "#hangupCallBtn"),
  };
  await plPage.screenshot({ path: path.join(DT_DIR, "dt_voice_3_urgent.png") });
  console.log("Captured dt_voice_3_urgent.png");

  await plCtx.close();

  // ==================================================================
  // Captures 7 & 8: Authentic WhatsApp Web Mockup (1600x960 @ 2x)
  // ==================================================================
  const waCtx = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    colorScheme: "dark",
  });
  const waPage = await waCtx.newPage();
  const waFileUrl = pathToFileURL(path.join(DT_DIR, "whatsapp_mock.html")).href;

  // 7) dt_whatsapp_1_bp.png
  await waPage.goto(`${waFileUrl}?step=1`, { waitUntil: "load" });
  await waPage.waitForTimeout(250);
  coordsReport["dt_whatsapp_1_bp.png"] = {
    bpCard: await measureCenter(waPage, ".bubble.bp-card"),
  };
  await waPage.screenshot({ path: path.join(DT_DIR, "dt_whatsapp_1_bp.png") });
  console.log("Captured dt_whatsapp_1_bp.png");

  // 8) dt_whatsapp_2_ticket.png
  await waPage.goto(`${waFileUrl}?step=2`, { waitUntil: "load" });
  await waPage.waitForTimeout(250);
  coordsReport["dt_whatsapp_2_ticket.png"] = {
    ticketPortalLink: await measureCenter(waPage, "#ticketPortalLink"),
    urgentBubble: await measureCenter(waPage, ".bubble.urgent-card"),
  };
  await waPage.screenshot({ path: path.join(DT_DIR, "dt_whatsapp_2_ticket.png") });
  console.log("Captured dt_whatsapp_2_ticket.png");

  await waCtx.close();

  if (needPortal) {
    // 9) dt_ticket_portal.png
    const ticketCtx = await browser.newContext({
      viewport: VIEWPORT,
      deviceScaleFactor: 2,
      colorScheme: "dark",
    });
    const tPage = await ticketCtx.newPage();
    await tPage.goto(`${BASE_URL}/ticket/SP-1042`, { waitUntil: "networkidle" });
    await tPage.waitForSelector(".container");

    await tPage.evaluate(() => {
      const symptomP = document.querySelector(".card-alt p");
      if (symptomP) {
        const cur = (symptomP.textContent || "").trim();
        const desired = '"Severe chest pain and heart palpitations reported during voice triage."';
        if (!cur.toLowerCase().includes("severe chest pain and heart palpitations")) {
          symptomP.textContent = desired;
        }
      }

      document.body.style.padding = "18px 16px";
      document.body.style.minHeight = "100vh";
      document.body.style.display = "flex";
      document.body.style.alignItems = "center";
      document.body.style.justifyContent = "center";

      const c = document.querySelector(".container");
      if (c) {
        c.style.width = "100%";
        const rect = c.getBoundingClientRect();
        const availH = window.innerHeight - 36;
        if (rect.height > availH) {
          const z = Math.max(0.75, +(availH / rect.height).toFixed(3));
          document.body.style.zoom = String(z);
        }
      }
      window.scrollTo(0, 0);
    });
    await tPage.waitForTimeout(300);

    coordsReport["dt_ticket_portal.png"] = {
      emergencyBtn: await measureCenter(tPage, ".btn-emergency"),
      whatsappBtn: await measureCenter(tPage, ".btn-wa"),
      container: await measureCenter(tPage, ".container"),
    };
    await tPage.screenshot({ path: path.join(DT_DIR, "dt_ticket_portal.png") });
    console.log("Captured dt_ticket_portal.png");

    await ticketCtx.close();
  } else {
    console.log("Keeping existing dt_ticket_portal.png");
  }
} finally {
  await browser.close();
}

function readPngDimensions(filePath) {
  const buf = fs.readFileSync(filePath);
  const width = buf.readUInt32BE(16);
  const height = buf.readUInt32BE(20);
  return { width, height, bytes: buf.length };
}

const dimsReport = {};
for (const fname of [
  "dt_hover_0_avatar.png",
  "dt_chat_welcome.png",
  "dt_chat_kb.png",
  "dt_chat_workflow.png",
  "dt_voice_1_dial.png",
  "dt_voice_2_bp.png",
  "dt_voice_3_urgent.png",
  "dt_whatsapp_1_bp.png",
  "dt_whatsapp_2_ticket.png",
  "dt_ticket_portal.png",
]) {
  dimsReport[fname] = readPngDimensions(path.join(DT_DIR, fname));
}

console.log("\n=== PNG DIMENSIONS & SIZES ===");
console.log(JSON.stringify(dimsReport, null, 2));
console.log("\n=== NORMALIZED CURSOR COORDINATES (1600x960 -> 0..1) ===");
console.log(JSON.stringify(coordsReport, null, 2));
