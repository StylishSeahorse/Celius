// Bundled connect page. The chat UI itself is the web app served by your Celius server, so
// the desktop app never needs its own copy of the UI or any API credentials: the window
// simply navigates to the server, where the normal session cookie applies. The address is
// stored by the native side, which also scopes the server's access to desktop features.
const invoke = (cmd, args) => window.__TAURI_INTERNALS__.invoke(cmd, args);
const params = new URLSearchParams(location.search);
const form = document.getElementById("connect");
const input = document.getElementById("url");
const error = document.getElementById("error");
const submit = document.getElementById("submit");

function normalise(raw) {
  let value = raw.trim();
  if (!/^https?:\/\//i.test(value)) value = `http://${value}`;
  const url = new URL(value);
  return url.origin;
}

async function reachable(origin) {
  // The page is cross-origin to the server, so only reachability can be observed.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 6000);
  try {
    await fetch(`${origin}/api/v1/health/live`, { mode: "no-cors", signal: controller.signal });
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

invoke("get_server").then((saved) => {
  if (saved && !params.has("change")) {
    location.replace(saved);
  } else {
    if (saved) input.value = saved;
    input.focus();
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  let origin;
  try {
    origin = normalise(input.value);
  } catch {
    error.textContent = "That doesn't look like a web address.";
    error.hidden = false;
    return;
  }
  submit.disabled = true;
  submit.textContent = "Connecting…";
  if (!(await reachable(origin))) {
    error.textContent = `Couldn't reach ${origin}. Check the address and that the server is running.`;
    error.hidden = false;
    submit.disabled = false;
    submit.textContent = "Connect";
    return;
  }
  try {
    location.replace(await invoke("set_server", { url: origin }));
  } catch (err) {
    error.textContent = String(err);
    error.hidden = false;
    submit.disabled = false;
    submit.textContent = "Connect";
  }
});
