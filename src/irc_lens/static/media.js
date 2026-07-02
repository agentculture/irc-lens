// irc-lens click-to-load media embeds.
//
// Wires a delegated click listener on the #chat-log container to handle
// click-to-load media buttons. When a user clicks a lens-media-load button,
// the module reads data-src and data-kind, validates the URL, builds an
// img or audio element, and swaps it in place of the button.
(function () {
  "use strict";

  const log = document.getElementById("chat-log");
  if (!log) return; // Guard: safe on pages without chat-log

  log.addEventListener("click", function (e) {
    const button = e.target.closest(".lens-media-load");
    if (!button) return;

    // .dataset, not get/setAttribute, for data-* reads (S7761).
    const src = button.dataset.src;
    const kind = button.dataset.kind;

    if (!src || !kind) return;

    // Scheme check on a lowercased copy; element below keeps ORIGINAL casing.
    const lowerSrc = src.toLowerCase();
    if (!lowerSrc.startsWith("http://") && !lowerSrc.startsWith("https://")) {
      return;
    }

    // Build the appropriate element based on kind
    let el;
    if (kind === "image") {
      el = document.createElement("img");
      el.setAttribute("src", src);
      el.setAttribute("loading", "lazy");
    } else if (kind === "audio") {
      el = document.createElement("audio");
      el.setAttribute("src", src);
      el.setAttribute("controls", "");
      el.setAttribute("preload", "metadata");
    } else {
      // Unknown kind: do nothing
      return;
    }

    // `class` stays setAttribute (not data-*); rest use .dataset (S7761).
    el.setAttribute("class", "lens-media-item");
    el.dataset.kind = kind;
    el.dataset.testid = "media-embed";

    // Replace button with element
    button.replaceWith(el);

    // Swap wrapper's data-testid; S6582: optional chaining, not `a && a.b`.
    const wrapper = el.parentElement;
    if (wrapper?.classList.contains("lens-media")) {
      wrapper.dataset.testid = "media-embed";
    }
  });
})();

// irc-lens upload UI (task t8): attach button, drag-drop, paste, auto-send.
//
// All three input surfaces funnel through ONE send path: POST the file to
// /upload, and on 201 set the existing #chat-input value to the returned
// capability URL and call requestSubmit() on #chat-form — the exact same
// HTMX POST /input pipeline a typed message already uses. Never POST
// /input directly from here. Upload failures reuse lens.js's toast DOM
// pattern (#toast-region, .lens-toast, role="alert") via a tiny local
// helper — see that file for the canonical implementation this mirrors.
(function () {
  "use strict";

  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const chatArea = document.getElementById("chat-log");
  const attachButton = document.querySelector('[data-testid="media-attach"]');
  const fileInput = document.querySelector('[data-testid="media-file-input"]');

  // S4144: the ONE toast implementation in this file. Exposed below via
  // globalThis.LensMedia.showToast so the recording IIFE (task t9) reuses
  // it instead of redefining an identical copy.
  function showToast(message) {
    const toasts = document.getElementById("toast-region");
    if (!toasts) return;
    toasts.setAttribute("aria-live", "assertive");
    const el = document.createElement("div");
    el.className = "lens-toast lens-toast--error";
    el.setAttribute("role", "alert");
    el.textContent = message;
    toasts.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  // Upload `file` to /upload; on 201, submit the returned URL through the
  // existing chat-input/#chat-form pipeline. On failure, parse the
  // {error, hint} body and toast it. Same-origin fetch, so the Origin
  // header the CSRF check requires is sent automatically by the browser.
  async function uploadAndSend(file) {
    if (!file || !form || !input) return;
    const body = new FormData();
    body.append("file", file);
    let resp;
    try {
      resp = await fetch("/upload", { method: "POST", body });
    } catch (_err) {
      // best-effort: fetch failed; toast covers it, _err not actionable. (S2486)
      showToast("upload failed: network error");
      return;
    }
    if (resp.status !== 201) {
      let payload = {};
      try {
        payload = await resp.json();
      } catch (_err) {
        // best-effort: non-JSON error body; fall back to status-only. (S2486)
      }
      let msg = payload.error || "upload failed (" + resp.status + ")";
      if (payload.hint) msg += " — " + payload.hint;
      showToast(msg);
      return;
    }
    const data = await resp.json();
    input.value = data.url;
    form.requestSubmit();
  }

  // Expose the upload-then-send helper and the shared toast helper so
  // other media.js modules (task t9's mic-recording IIFE below) can reuse
  // them instead of duplicating — mirrors how mesh.js exposes
  // `globalThis.LensMesh` (S7764: globalThis, not window).
  globalThis.LensMedia = { uploadAndSend: uploadAndSend, showToast: showToast };

  // Attach button opens the hidden file picker; picking a file uploads it.
  if (attachButton && fileInput) {
    attachButton.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const file = fileInput.files?.[0];
      fileInput.value = ""; // reset so re-picking the same file still fires change
      if (file) uploadAndSend(file);
    });
  }

  // Drag-drop onto the chat log. preventDefault on dragover is required
  // for the browser to fire drop at all.
  if (chatArea) {
    chatArea.addEventListener("dragover", (e) => e.preventDefault());
    chatArea.addEventListener("drop", (e) => {
      e.preventDefault();
      const files = e.dataTransfer?.files;
      const file = files?.[0];
      if (file) uploadAndSend(file);
    });
  }

  // Paste an image while focused on the message input.
  if (input) {
    input.addEventListener("paste", (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      // S4138: for-of, since the index was only ever used for items[i].
      for (const item of items) {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) {
            e.preventDefault();
            uploadAndSend(file);
          }
          break;
        }
      }
    });
  }
})();

// irc-lens mic recording (task t9): getUserMedia + MediaRecorder capture.
// Button-text states: idle -> recording (elapsed seconds, click again to
// stop) -> uploading -> idle. Hard-caps at 300s, closes every stream
// track on stop, and sends the file via globalThis.LensMedia.uploadAndSend
// — the SAME send path file uploads use above, so there's only one.
(function () {
  "use strict";

  const recordButton = document.querySelector('[data-testid="media-record"]');
  if (!recordButton) return; // Guard: safe on pages without the button

  const IDLE_LABEL = "🎙 record";
  const MAX_SECONDS = 300;
  const OPUS_MIME = "audio/webm;codecs=opus";
  let state = "idle"; // idle | recording | uploading
  let mediaRecorder = null;
  let activeStream = null;
  let chunks = [];
  let timerId = null;
  let elapsedSeconds = 0;
  let recordingExt = "webm";

  // S4144: no local toast copy here — reuse the single implementation the
  // upload IIFE above exposes on globalThis.LensMedia.showToast.
  const toast = globalThis.LensMedia.showToast;

  function stopStreamTracks() {
    if (!activeStream) return;
    activeStream.getTracks().forEach((track) => track.stop());
    activeStream = null;
  }
  function clearTimer() {
    clearInterval(timerId);
    timerId = null;
  }
  function resetToIdle() {
    state = "idle";
    elapsedSeconds = 0;
    recordButton.disabled = false;
    recordButton.textContent = IDLE_LABEL;
    clearTimer();
  }
  // Prefer opus when isTypeSupported says so; audio/mp4 is the fallback.
  function pickMimeType() {
    const supportsOpus =
      typeof MediaRecorder !== "undefined" &&
      MediaRecorder.isTypeSupported?.(OPUS_MIME);
    return supportsOpus
      ? { mimeType: OPUS_MIME, ext: "webm" }
      : { mimeType: "audio/mp4", ext: "m4a" };
  }

  async function onRecordingStopped() {
    stopStreamTracks();
    state = "uploading";
    recordButton.textContent = "uploading…";
    const mimeType = mediaRecorder?.mimeType || "audio/webm";
    const blob = new Blob(chunks, { type: mimeType });
    chunks = [];
    const filename = recordingExt === "webm" ? "recording.webm" : "recording.m4a";
    const file = new File([blob], filename, { type: mimeType });
    try {
      if (globalThis.LensMedia?.uploadAndSend) {
        await globalThis.LensMedia.uploadAndSend(file);
      }
    } catch (_err) {
      // best-effort: upload-after-recording failed; toast covers it. (S2486)
      toast("recording upload failed: network error");
    } finally {
      resetToIdle();
    }
  }

  function stopRecording() {
    clearTimer();
    if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
  }

  function tick() {
    elapsedSeconds += 1;
    recordButton.textContent = "⏹ " + elapsedSeconds + "s (click to stop)";
    if (elapsedSeconds >= MAX_SECONDS) stopRecording();
  }

  async function startRecording() {
    const noApi = typeof MediaRecorder === "undefined" || !navigator.mediaDevices?.getUserMedia;
    if (noApi) {
      toast("recording failed — hint: this browser has no mic/MediaRecorder support");
      return;
    }
    try {
      activeStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (_err) {
      // best-effort: getUserMedia rejected (e.g. permission denied). (S2486)
      toast("recording failed — hint: microphone permission was denied");
      return;
    }
    const chosen = pickMimeType();
    recordingExt = chosen.ext;
    try {
      mediaRecorder = new MediaRecorder(activeStream, { mimeType: chosen.mimeType });
    } catch (_err) {
      // best-effort: MediaRecorder construction failed; clean up and toast. (S2486)
      stopStreamTracks();
      toast("recording failed — hint: could not start MediaRecorder");
      return;
    }
    chunks = [];
    mediaRecorder.addEventListener("dataavailable", (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    });
    mediaRecorder.addEventListener("stop", onRecordingStopped);
    mediaRecorder.start();
    state = "recording";
    elapsedSeconds = 0;
    recordButton.textContent = "⏹ 0s (click to stop)";
    timerId = setInterval(tick, 1000);
  }

  recordButton.addEventListener("click", () => {
    if (state === "idle") startRecording();
    else if (state === "recording") stopRecording();
    // state === "uploading": ignore clicks until the send path resolves.
  });
})();
