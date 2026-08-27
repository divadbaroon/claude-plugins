// A stand-in for berkeley.mathetic.com, so the real CLI and the real
// credential helper can be driven through every state the pool can be in
// without a browser, a deployment, or a cent of credit.
//
//   node fake-engelbart.js            # healthy, credit available
//   STATE=exhausted node fake-...     # credit spent
//
// Flip STATE while it runs by writing to the file named in STATE_FILE.
const http = require('http');
const fs = require('fs');

const STATE_FILE = process.env.STATE_FILE || '/tmp/engelbart-fake-state';
function state() {
  try { return fs.readFileSync(STATE_FILE, 'utf8').trim(); } catch { return process.env.STATE || 'active'; }
}

function body(req) {
  return new Promise((resolve) => {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => { try { resolve(JSON.parse(raw || '{}')); } catch { resolve({}); } });
  });
}

const server = http.createServer(async (req, res) => {
  const send = (status, value) => {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(value));
  };
  const url = req.url.split('?')[0];

  if (url === '/api/engelbart-device') {
    const input = await body(req);
    if (input.action === 'start') {
      console.log('  → device flow started; approving automatically');
      return send(200, {
        deviceCode: 'dev-code', userCode: 'TEST-0000',
        expiresInSeconds: 600, intervalSeconds: 1,
      });
    }
    if (input.action === 'poll') return send(200, { status: 'ready', token: 'egb_test_token', email: 'you@example.com' });
    if (input.action === 'whoami') {
      console.log('  → whoami (re-wire path: no browser needed)');
      return send(200, { email: 'you@example.com' });
    }
    if (input.action === 'revoke') return send(200, { revoked: true });
    return send(400, { error: 'unknown action' });
  }

  if (url === '/api/engelbart-credentials') {
    const now = state();
    console.log(`  → credentials asked for; answering "${now}"`);
    if (now === 'exhausted') {
      return send(200, {
        status: 'exhausted', apiKey: 'sk-test-DEAD', baseUrl: 'https://proxy.example.com',
        budgetUsd: 25, spendUsd: 25,
      });
    }
    if (now === 'down') return send(503, { error: 'upstream unavailable' });
    return send(200, {
      status: 'active', apiKey: 'sk-test-LIVE', baseUrl: 'https://proxy.example.com',
      budgetUsd: 25, spendUsd: 4,
    });
  }

  if (url === '/api/engelbart-config') return send(200, {});
  return send(404, { error: 'not found' });
});

server.listen(4567, '127.0.0.1', () => {
  console.log('fake Engelbart API on http://127.0.0.1:4567');
  console.log(`state file: ${STATE_FILE} (write "active", "exhausted", or "down")`);
});
