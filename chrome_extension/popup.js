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

var parseBtn = document.getElementById('parse-btn');
var parseSpinner = document.getElementById('parse-spinner');
var textBtn = document.getElementById('text-parse-btn');
var textSpinner = document.getElementById('text-spinner');

parseBtn.addEventListener('click', function() {
  setBusy(parseBtn, parseSpinner, true);

  chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
    var activeTab = tabs[0];
    chrome.tabs.sendMessage(activeTab.id, {"message": "parse_url"}, function(response) {
      setBusy(parseBtn, parseSpinner, false);
      if (chrome.runtime.lastError) {
        showStatus('error', 'Could not reach the page', chrome.runtime.lastError.message);
        return;
      }
      if (response && response.error) {
        showStatus('error', 'Parsing failed', response.error);
      } else if (response && response.data) {
        showResult(response.data);
      }
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

      chrome.tabs.sendMessage(activeTab.id, {message: 'vp_show', text: 'Parsing selected text…'});

      fetch(`http://127.0.0.1:8000/api/parse_text/`, {
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
        if (data.status === 'success') {
          chrome.tabs.sendMessage(activeTab.id, {
            message: 'vp_status', kind: 'success',
            text: `Saved: ${data.job_title || 'vacancy'} @ ${data.company_name || ''}`,
          });
        } else {
          chrome.tabs.sendMessage(activeTab.id, {
            message: 'vp_status', kind: 'error', text: data.error || 'Parsing failed',
          });
        }
      })
      .catch(error => {
        setBusy(textBtn, textSpinner, false);
        showStatus('error', 'Parsing failed', error.message);
        chrome.tabs.sendMessage(activeTab.id, {message: 'vp_status', kind: 'error', text: error.message});
      });
    });
  });
});
