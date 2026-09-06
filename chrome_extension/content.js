const VP_OVERLAY_ID = 'vp-vacancy-parser-overlay';

function vpEnsureStyles() {
  if (document.getElementById('vp-vacancy-parser-styles')) return;

  const style = document.createElement('style');
  style.id = 'vp-vacancy-parser-styles';
  style.textContent = `
    #${VP_OVERLAY_ID} {
      position: fixed;
      bottom: 20px;
      right: 20px;
      z-index: 2147483647;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 12px 16px;
      background: #1a1a1a;
      color: #fff;
      border-radius: 10px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.35);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      font-size: 13px;
      max-width: 320px;
      line-height: 1.4;
      transition: opacity 0.25s ease;
    }
    #${VP_OVERLAY_ID}.vp-success { background: #14432a; }
    #${VP_OVERLAY_ID}.vp-error { background: #4a1c1c; }
    #${VP_OVERLAY_ID} .vp-spinner {
      width: 16px;
      height: 16px;
      flex: none;
      border: 2px solid rgba(255,255,255,0.35);
      border-top-color: #fff;
      border-radius: 50%;
      animation: vp-spin 0.7s linear infinite;
    }
    @keyframes vp-spin { to { transform: rotate(360deg); } }
  `;
  document.head.appendChild(style);
}

function vpShowOverlay(text) {
  vpEnsureStyles();
  let overlay = document.getElementById(VP_OVERLAY_ID);
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = VP_OVERLAY_ID;
    document.body.appendChild(overlay);
  }
  overlay.className = '';
  overlay.style.opacity = '1';
  overlay.innerHTML = `<span class="vp-spinner"></span><span>${text}</span>`;
}

function vpSetOverlayStatus(kind, text) {
  const overlay = document.getElementById(VP_OVERLAY_ID);
  if (!overlay) return;
  overlay.className = kind === 'success' ? 'vp-success' : 'vp-error';
  overlay.innerHTML = `<span>${kind === 'success' ? '✅' : '⚠️'}</span><span>${text}</span>`;
}

function vpHideOverlay(delay) {
  const overlay = document.getElementById(VP_OVERLAY_ID);
  if (!overlay) return;
  setTimeout(() => {
    overlay.style.opacity = '0';
    setTimeout(() => overlay.remove(), 250);
  }, delay || 0);
}

// This content script is now purely the on-page overlay UI. All network calls
// to the backend happen in popup.js (extension context) to avoid mixed-content
// blocking of http:// requests on https:// pages.
chrome.runtime.onMessage.addListener(function(request, sender, sendResponse) {
  if (request.message === 'vp_show') {
    vpShowOverlay(request.text || 'Working…');
    sendResponse({ ok: true });
  }

  if (request.message === 'vp_status') {
    vpSetOverlayStatus(request.kind, request.text);
    vpHideOverlay(request.kind === 'success' ? 3000 : 5000);
    sendResponse({ ok: true });
  }
});
