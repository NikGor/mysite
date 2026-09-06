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

var parseBtn = document.getElementById('parse-btn');
var parseSpinner = document.getElementById('parse-spinner');
var textBtn = document.getElementById('text-parse-btn');
var textSpinner = document.getElementById('text-spinner');

parseBtn.addEventListener('click', function() {
  setBusy(parseBtn, parseSpinner, true);

  chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
    var activeTab = tabs[0];
    var pageUrl = activeTab.url;
    overlayMessage(activeTab.id, {message: 'vp_show', text: 'Parsing this page…'});

    fetch(`${API_BASE}/api/parse_url/?url=${encodeURIComponent(pageUrl)}`)
      .then(response => {
        if (!response.ok) {
          throw new Error(`Request failed with status ${response.status}`);
        }
        return response.json();
      })
      .then(data => {
        setBusy(parseBtn, parseSpinner, false);
        showResult(data);
        notifyOverlayResult(activeTab.id, data);
      })
      .catch(error => {
        setBusy(parseBtn, parseSpinner, false);
        showStatus('error', 'Parsing failed', error.message);
        overlayMessage(activeTab.id, {message: 'vp_status', kind: 'error', text: error.message});
      });
  });
});

textBtn.addEventListener('click', function() {
  setBusy(textBtn, textSpinner, true);

  chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
    var activeTab = tabs[0];
    chrome.scripting.executeScript({
      target: {tabId: activeTab.id},
      func: () => window.getSelection().toString(),
    }, function(results) {
      var selectedText = results && results[0] && results[0].result;
      if (!selectedText) {
        setBusy(textBtn, textSpinner, false);
        showStatus('error', 'Nothing selected', 'Select the vacancy text on the page first.');
        return;
      }

      overlayMessage(activeTab.id, {message: 'vp_show', text: 'Parsing selected text…'});

      fetch(`${API_BASE}/api/parse_text/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({text: selectedText})
      })
      .then(response => {
        if (!response.ok) {
          throw new Error(`Request failed with status ${response.status}`);
        }
        return response.json();
      })
      .then(data => {
        setBusy(textBtn, textSpinner, false);
        showResult(data);
        notifyOverlayResult(activeTab.id, data);
      })
      .catch(error => {
        setBusy(textBtn, textSpinner, false);
        showStatus('error', 'Parsing failed', error.message);
        overlayMessage(activeTab.id, {message: 'vp_status', kind: 'error', text: error.message});
      });
    });
  });
});
