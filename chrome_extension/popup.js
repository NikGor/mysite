// Backend base URL. Points at the always-on Raspberry Pi (archie) on the LAN.
// All network calls happen here in the popup (an extension page) rather than in
// the content script, so http:// requests to the Pi aren't blocked as mixed
// content on https:// job pages. host_permissions in manifest.json cover this.
// Fallbacks if mDNS (.local) doesn't resolve on a device: http://192.168.0.234:8080
const API_BASE = 'http://mysite.local:8080';

function setBusy(button, spinner, busy) {
  button.disabled = busy;
  spinner.style.display = busy ? 'inline-block' : 'none';
}

function showStatus(kind, title, message) {
  var status = document.getElementById('status');
  status.className = kind;
  status.innerHTML = `<strong>${title}</strong>${message || ''}`;
}

function showResult(data) {
  if (data.status === 'success') {
    showStatus('success', `${data.job_title || 'Vacancy'} — ${data.company_name || ''}`, 'Saved to your job application dashboard.');
  } else {
    showStatus('error', 'Parsing failed', data.error || 'Unexpected response from the server.');
  }
}

// Best-effort overlay message to the content script; ignore pages where no
// content script is injected (chrome://, the web store, etc.).
function overlayMessage(tabId, payload) {
  try {
    chrome.tabs.sendMessage(tabId, payload, function() {
      void chrome.runtime.lastError; // swallow "no receiving end" errors
    });
  } catch (e) { /* no-op */ }
}

function notifyOverlayResult(tabId, data) {
  if (data.status === 'success') {
    overlayMessage(tabId, {
      message: 'vp_status', kind: 'success',
      text: `Saved: ${data.job_title || 'vacancy'} @ ${data.company_name || ''}`,
    });
  } else {
    overlayMessage(tabId, {message: 'vp_status', kind: 'error', text: data.error || 'Parsing failed'});
  }
}

// Rejects with the server's own `error` message when there is one, so the popup
// shows "Could not fetch …" instead of a bare status code.
function requestJSON(url, options) {
  return fetch(url, options).then(response =>
    response.json().catch(() => ({})).then(data => {
      if (!response.ok) {
        throw new Error(data.error || `Request failed with status ${response.status}`);
      }
      return data;
    })
  );
}

function parseText(text, pageUrl) {
  return requestJSON(`${API_BASE}/api/parse_text/`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: text, url: pageUrl || null}),
  });
}

function runParse(button, spinner, tabId, request) {
  request
    .then(data => {
      setBusy(button, spinner, false);
      showResult(data);
      notifyOverlayResult(tabId, data);
    })
    .catch(error => {
      setBusy(button, spinner, false);
      showStatus('error', 'Parsing failed', error.message);
      overlayMessage(tabId, {message: 'vp_status', kind: 'error', text: error.message});
    });
}

// Reads text from the active tab; resolves to '' when the page can't be scripted
// (chrome://, the web store, etc.).
function readFromTab(tabId, func) {
  return new Promise(resolve => {
    chrome.scripting.executeScript({target: {tabId: tabId}, func: func}, function(results) {
      void chrome.runtime.lastError;
      resolve((results && results[0] && results[0].result) || '');
    });
  });
}

var parseBtn = document.getElementById('parse-btn');
var parseSpinner = document.getElementById('parse-spinner');
var textBtn = document.getElementById('text-parse-btn');
var textSpinner = document.getElementById('text-spinner');

// Sends the text the browser already rendered (with the user's session) instead of
// letting the server re-fetch the URL: LinkedIn answers 429 to anonymous requests
// from the Pi. Server-side fetch stays only as a fallback for unscriptable pages.
parseBtn.addEventListener('click', function() {
  setBusy(parseBtn, parseSpinner, true);

  chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
    var activeTab = tabs[0];
    var pageUrl = activeTab.url;
    overlayMessage(activeTab.id, {message: 'vp_show', text: 'Parsing this page…'});

    readFromTab(activeTab.id, () => document.body.innerText).then(pageText => {
      var request = pageText
        ? parseText(pageText, pageUrl)
        : requestJSON(`${API_BASE}/api/parse_url/?url=${encodeURIComponent(pageUrl)}`);
      runParse(parseBtn, parseSpinner, activeTab.id, request);
    });
  });
});

textBtn.addEventListener('click', function() {
  setBusy(textBtn, textSpinner, true);

  chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
    var activeTab = tabs[0];
    readFromTab(activeTab.id, () => window.getSelection().toString()).then(selectedText => {
      if (!selectedText) {
        setBusy(textBtn, textSpinner, false);
        showStatus('error', 'Nothing selected', 'Select the vacancy text on the page first.');
        return;
      }
      overlayMessage(activeTab.id, {message: 'vp_show', text: 'Parsing selected text…'});
      runParse(textBtn, textSpinner, activeTab.id,
        parseText(selectedText, activeTab.url));
    });
  });
});
