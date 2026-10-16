// Render a JavaScript page (e.g. a Streamlit app) headless and save what it shows:
// screenshot, visible text, Plotly/Vega chart data and every JSON/CSV network response.
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const [url, out, waitMs] = [process.argv[2], process.argv[3], +(process.argv[4] || 45000)];
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
  let n = 0;
  page.on('response', async r => {
    try {
      const ct = r.headers()['content-type'] || '';
      if (/json|csv|text\/plain/.test(ct)) fs.writeFileSync(`${out}/resp_${++n}.txt`, r.url() + '\n' + await r.text());
    } catch (e) {}
  });
  const ws = [];
  page.on('websocket', s => s.on('framereceived', f => ws.push(typeof f.payload === 'string' ? f.payload : Buffer.from(f.payload).toString('base64'))));
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(waitMs);
  await page.screenshot({ path: `${out}/page.png`, fullPage: true });
  fs.writeFileSync(`${out}/text.txt`, await page.evaluate(() => document.body.innerText));
  fs.writeFileSync(`${out}/plotly.json`, JSON.stringify(await page.evaluate(() =>
    [...document.querySelectorAll('.js-plotly-plot')].map(d => ({ data: d.data, layout: d.layout })))));
  fs.writeFileSync(`${out}/frames.json`, JSON.stringify(page.frames().map(f => f.url())));
  fs.writeFileSync(`${out}/ws_frames.txt`, ws.join('\n'));
  fs.writeFileSync(`${out}/dom.html`, await page.content());
  await browser.close();
})();
