/**
 * AI Phone Agent -- Browser Client
 *
 * Handles:
 *  - WebSocket connection to the backend
 *  - Microphone audio capture and encoding
 *  - Provider selection (Twilio / Exotel)
 *  - Live call display with sequential audio playback
 *  - Agent activity visualization
 */

// ------------------------------------------------
// State
// ------------------------------------------------

var ws = null;
var mediaRecorder = null;
var audioChunks = [];
var isRecording = false;
var currentCallId = null;
var callInProgress = false;
var audioQueue = [];
var isPlayingAudio = false;
var audioMuted = false;
var selectedProvider = "exotel";

// ------------------------------------------------
// DOM Elements
// ------------------------------------------------

var $ = function (sel) { return document.querySelector(sel); };

var conversation = $("#conversation");
var textInput = $("#textInput");
var sendBtn = $("#sendBtn");
var micBtn = $("#micBtn");
var micLabel = $("#micLabel");
var clearBtn = $("#clearBtn");
var muteBtn = $("#muteBtn");
var statusBadge = $("#statusBadge");
var statusText = $(".status-text");
var callStateEl = $("#callState");
var callSid = $("#callSid");
var twilioSid = $("#twilioSid");
var callDuration = $("#callDuration");
var intentSection = $("#intentSection");
var intentCard = $("#intentCard");
var agentLog = $("#agentLog");
var callSection = $("#callSection");
var startCallBtn = $("#startCallBtn");
var endCallBtn = $("#endCallBtn");
var userNameInput = $("#userName");
var userPhoneInput = $("#userPhone");
var userDobInput = $("#userDob");
var userAgeInput = $("#userAge");
var userGenderInput = $("#userGender");
var userWeightInput = $("#userWeight");
var userHeightInput = $("#userHeight");
var callHint = $("#callHint");
var dtmfSection = $("#dtmfSection");

// ------------------------------------------------
// WebSocket Connection
// ------------------------------------------------

function connect() {
  var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  var wsUrl = protocol + "//" + window.location.host + "/ws/browser";

  ws = new WebSocket(wsUrl);

  ws.onopen = function () {
    console.log("[WS] Connected");
    updateStatus("connected", "Connected");
  };

  ws.onmessage = function (event) {
    var msg = JSON.parse(event.data);
    handleMessage(msg);
  };

  ws.onclose = function () {
    console.log("[WS] Disconnected");
    updateStatus("disconnected", "Disconnected");
    setTimeout(connect, 3000);
  };

  ws.onerror = function (err) {
    console.error("[WS] Error:", err);
    updateStatus("disconnected", "Error");
  };
}

function handleMessage(msg) {
  var type = msg.type;
  var data = msg.data;

  switch (type) {
    case "call_status":
      handleCallStatus(data);
      break;
    case "transcript":
      handleTranscript(data);
      break;
    case "ready_for_call":
      handleReadyForCall(data);
      break;
    case "call_turn":
      handleCallTurn(data);
      break;
    case "call_complete":
      handleCallComplete(data);
      break;
    case "agent_update":
      handleAgentUpdate(data);
      break;
    case "error":
      addMessage("error", "Error", data.message);
      if (callInProgress) {
        callInProgress = false;
        startCallBtn.disabled = false;
        startCallBtn.querySelector("span").textContent = "Start Call";
      }
      break;
  }
}

// ------------------------------------------------
// Message Handlers
// ------------------------------------------------

function handleCallStatus(data) {
  var status = data.status;
  var message = data.message;
  var intent = data.intent;
  var call_id = data.call_id;

  callStateEl.textContent = status
    .replace(/_/g, " ")
    .replace(/^\w/, function (c) { return c.toUpperCase(); });

  if (call_id) {
    currentCallId = call_id;
    callSid.textContent = call_id.substring(0, 16) + "...";
  }

  var sid = data.provider_sid || data.twilio_sid || data.exotel_sid;
  if (sid) {
    twilioSid.textContent = sid.substring(0, 20) + "...";
    twilioSid.title = sid;
  }

  // Populate profile fields with defaults from backend (only if empty)
  if (data.default_name && !userNameInput.value) {
    userNameInput.value = data.default_name;
  }
  if (data.default_phone && !userPhoneInput.value) {
    userPhoneInput.value = data.default_phone;
  }
  if (data.default_dob && !userDobInput.value) {
    userDobInput.value = data.default_dob;
  }
  if (data.default_age && !userAgeInput.value) {
    userAgeInput.value = data.default_age;
  }
  if (data.default_gender && !userGenderInput.value) {
    userGenderInput.value = data.default_gender;
  }
  if (data.default_weight && !userWeightInput.value) {
    userWeightInput.value = data.default_weight;
  }
  if (data.default_height && !userHeightInput.value) {
    userHeightInput.value = data.default_height;
  }

  if (intent) {
    showIntent(intent);
  }

  if (status === "processing") {
    updateAgentLog(1, "Processing user input...", true);
  } else if (status === "intent_extracted") {
    updateAgentLog(1, "Intent: " + message, true);
  } else if (status === "calling") {
    updateStatus("calling", "Dialing...");
    callInProgress = true;
    var prov = (data.provider || selectedProvider || "").charAt(0).toUpperCase() +
      (data.provider || selectedProvider || "").slice(1);
    updateAgentLog(2, "Initiating " + prov + " call...", true);
  } else if (status === "ringing") {
    updateStatus("calling", "Ringing");
    updateAgentLog(2, "Phone ringing...", true);
  } else if (status === "phone_connected") {
    updateStatus("calling", "Phone Connected");
    updateAgentLog(2, "Phone connected, starting agents...", true);
  } else if (status === "in_call") {
    updateStatus("calling", "In Call");
    updateAgentLog(2, "Call connected", true);
    updateAgentLog(3, "Ready", false);
    // Show DTMF dialpad
    if (dtmfSection) dtmfSection.style.display = "block";
  } else if (status === "sms_sent") {
    updateStatus("connected", "SMS Sent");
  }

  addMessage("system", "System", message);
}

function handleTranscript(data) {
  if (data.role === "user") {
    addMessage("user", "You", data.text);
    updateAgentLog(1, "Transcribed: \"" + data.text.substring(0, 50) + "...\"", true);
  }
}

function handleReadyForCall(data) {
  currentCallId = data.call_id;
  callSection.classList.add("active");
  startCallBtn.style.display = "block";
  startCallBtn.disabled = false;
  endCallBtn.style.display = "none";
  addMessage("system", "System", data.message);
  callStateEl.textContent = "Ready";

  var target = data.target_entity || "target";
  var phone = data.target_phone || "";
  var provLabel = selectedProvider === "twilio" ? "Twilio" : "Exotel";

  callHint.textContent = phone
    ? "Will call " + target + " at " + phone + " via " + provLabel + "."
    : "Ready to call " + target + ".";
  startCallBtn.querySelector("span").textContent = "Call " + target;
}

function handleCallTurn(data) {
  var speaker = data.speaker;
  var text = data.text;
  var audioB64 = data.audio_b64;
  var turn = data.turn;

  var isOther = speaker !== "agent";
  var type = isOther ? "hospital" : "agent";
  var label = isOther ? "Other Party" : "Our Agent";

  addMessageWithAudio(type, label, text, audioB64);
  callDuration.textContent = String(turn);
}

function handleCallComplete(data) {
  callInProgress = false;
  callStateEl.textContent = "Complete";
  updateStatus("connected", "Done");

  addMessage(
    "system",
    "System",
    data.message + " (" + data.total_turns + " turns)"
  );

  updateAgentLog(1, "Idle", false);
  updateAgentLog(2, "Idle", false);
  updateAgentLog(3, "Idle", false);

  startCallBtn.disabled = false;
  startCallBtn.style.display = "block";
  startCallBtn.querySelector("span").textContent = "Start Call";
  endCallBtn.style.display = "none";
  // Hide DTMF dialpad
  if (dtmfSection) dtmfSection.style.display = "none";
}

function handleAgentUpdate(data) {
  updateAgentLog(data.agent, data.text, data.active);
}

// ------------------------------------------------
// UI -- Messages
// ------------------------------------------------

function addMessage(type, label, text) {
  var div = document.createElement("div");
  div.className = "message " + type;
  div.innerHTML =
    '<div class="msg-header">' +
    '<div class="msg-label">' + escapeHtml(label) + "</div>" +
    "</div>" +
    '<div class="msg-text">' + escapeHtml(text) + "</div>";
  conversation.appendChild(div);
  conversation.scrollTop = conversation.scrollHeight;
}

function addMessageWithAudio(type, label, text, audioB64) {
  var div = document.createElement("div");
  div.className = "message " + type;

  var hasAudio = audioB64 && audioB64.length > 0;
  var audioId = hasAudio
    ? "audio_" + Date.now() + "_" + Math.random().toString(36).substr(2, 5)
    : "";

  var html =
    '<div class="msg-header">' +
    '<div class="msg-label">' + escapeHtml(label) + "</div>" +
    (hasAudio
      ? '<button class="audio-play-btn" id="' + audioId + '" title="Play audio">&#9654;</button>'
      : "") +
    "</div>" +
    '<div class="msg-text">' + escapeHtml(text) + "</div>";

  div.innerHTML = html;
  conversation.appendChild(div);
  conversation.scrollTop = conversation.scrollHeight;

  if (hasAudio) {
    var audio = new Audio("data:audio/mpeg;base64," + audioB64);
    var btn = document.getElementById(audioId);

    if (btn) {
      btn.addEventListener("click", function () {
        audio.currentTime = 0;
        audio.play().catch(function () { });
        btn.classList.add("playing");
        audio.onended = function () {
          btn.classList.remove("playing");
        };
      });
    }

    // Auto-play via sequential queue
    queueAudio(audio, div, btn);
  }
}

// ------------------------------------------------
// Audio Queue -- Sequential Auto-Playback
// ------------------------------------------------

function queueAudio(audio, msgEl, btn) {
  audioQueue.push({ audio: audio, msgEl: msgEl, btn: btn });
  if (!isPlayingAudio) {
    playNext();
  }
}

function playNext() {
  if (audioQueue.length === 0) {
    isPlayingAudio = false;
    return;
  }

  if (audioMuted) {
    audioQueue.shift();
    playNext();
    return;
  }

  isPlayingAudio = true;
  var item = audioQueue.shift();

  item.msgEl.classList.add("speaking");
  if (item.btn) { item.btn.classList.add("playing"); }

  item.audio.onended = function () {
    item.msgEl.classList.remove("speaking");
    if (item.btn) { item.btn.classList.remove("playing"); }
    playNext();
  };

  item.audio.onerror = function () {
    item.msgEl.classList.remove("speaking");
    if (item.btn) { item.btn.classList.remove("playing"); }
    playNext();
  };

  item.audio.play().catch(function () {
    item.msgEl.classList.remove("speaking");
    if (item.btn) { item.btn.classList.remove("playing"); }
    playNext();
  });
}

// ------------------------------------------------
// UI -- Status, Intent, Agent Log
// ------------------------------------------------

function updateStatus(state, text) {
  statusBadge.className = "status-badge " + state;
  statusText.textContent = text;
}

function showIntent(intent) {
  intentSection.style.display = "block";
  var fields = [
    ["Intent", intent.intent],
    ["Target", intent.target_entity],
    ["Target Phone", intent.target_phone],
    ["Task", intent.task_description],
    ["Hospital", intent.hospital_name],
    ["Branch", intent.hospital_branch],
    ["City", intent.hospital_city],
    ["Specialty", intent.doctor_specialty],
    ["Doctor", intent.doctor_name],
    ["Date", intent.appointment_date],
    ["Name", intent.user_name],
    ["Phone", intent.user_phone],
    ["Language", intent.detected_language],
  ].filter(function (pair) { return pair[1]; });

  intentCard.innerHTML = fields
    .map(function (pair) {
      return (
        '<div class="intent-row">' +
        '<span class="intent-key">' + pair[0] + "</span>" +
        '<span class="intent-val">' + escapeHtml(String(pair[1])) + "</span>" +
        "</div>"
      );
    })
    .join("");
}

function updateAgentLog(agentNum, text, active) {
  var badges = ["A1", "A2", "A3"];
  var classes = ["a1", "a2", "a3"];
  var entries = agentLog.querySelectorAll(".agent-entry");

  if (entries[agentNum - 1]) {
    entries[agentNum - 1].className =
      "agent-entry " + (active ? "active" : "idle");
    entries[agentNum - 1].innerHTML =
      '<span class="agent-badge ' + classes[agentNum - 1] + '">' +
      badges[agentNum - 1] +
      "</span>" +
      "<span>" + escapeHtml(text) + "</span>";
  }
}

function escapeHtml(str) {
  var div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ------------------------------------------------
// User Input
// ------------------------------------------------

function sendText() {
  var text = textInput.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

  ws.send(JSON.stringify({
    type: "user_text",
    data: {
      text: text,
      user_name: userNameInput.value.trim(),
      user_phone: userPhoneInput.value.trim(),
      user_dob: userDobInput.value.trim(),
      user_age: userAgeInput.value.trim(),
      user_gender: userGenderInput.value.trim(),
      user_weight: userWeightInput.value.trim(),
      user_height: userHeightInput.value.trim(),
    },
  }));
  textInput.value = "";
}

function toggleRecording() {
  if (isRecording) {
    stopRecording();
  } else {
    startRecording();
  }
}

function startRecording() {
  navigator.mediaDevices
    .getUserMedia({
      audio: {
        sampleRate: 16000,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    })
    .then(function (stream) {
      mediaRecorder = new MediaRecorder(stream, {
        mimeType: "audio/webm;codecs=opus",
      });

      audioChunks = [];

      mediaRecorder.ondataavailable = function (event) {
        if (event.data.size > 0) { audioChunks.push(event.data); }
      };

      mediaRecorder.onstop = function () {
        var blob = new Blob(audioChunks, { type: "audio/webm" });
        blob.arrayBuffer().then(function (buffer) {
          var base64 = btoa(
            new Uint8Array(buffer).reduce(function (data, byte) {
              return data + String.fromCharCode(byte);
            }, "")
          );

          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
              type: "user_audio",
              data: {
                audio: base64,
                user_name: userNameInput.value.trim(),
                user_phone: userPhoneInput.value.trim(),
                user_dob: userDobInput.value.trim(),
                user_age: userAgeInput.value.trim(),
                user_gender: userGenderInput.value.trim(),
                user_weight: userWeightInput.value.trim(),
                user_height: userHeightInput.value.trim(),
              },
            }));
          }
        });

        stream.getTracks().forEach(function (track) { track.stop(); });
      };

      mediaRecorder.start();
      isRecording = true;
      micBtn.classList.add("recording");
      micLabel.textContent = "Stop";
      addMessage("system", "System", "Recording... Click Stop when done.");
    })
    .catch(function (err) {
      console.error("Microphone error:", err);
      addMessage("error", "Error", "Could not access microphone.");
    });
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }
  isRecording = false;
  micBtn.classList.remove("recording");
  micLabel.textContent = "Record";
}

// ------------------------------------------------
// Start Call
// ------------------------------------------------

function startCall() {
  if (!currentCallId) return;

  startCallBtn.disabled = true;
  startCallBtn.style.display = "none";
  startCallBtn.querySelector("span").textContent = "Calling...";
  endCallBtn.style.display = "block";
  endCallBtn.disabled = false;
  endCallBtn.querySelector("span").textContent = "End Call";
  callInProgress = true;

  audioQueue = [];
  isPlayingAudio = false;

  addMessage("system", "System", "Starting call...");

  fetch("/api/start-call/" + currentCallId + "?provider=" + selectedProvider, { method: "POST" })
    .then(function (resp) {
      if (!resp.ok) {
        return resp.json().then(function (body) {
          addMessage("error", "Error", body.error || "Failed to start call.");
          startCallBtn.disabled = false;
          startCallBtn.querySelector("span").textContent = "Start Call";
          callInProgress = false;
        });
      }
      // Results come through WebSocket
    })
    .catch(function (err) {
      console.error("Start call error:", err);
      addMessage("error", "Error", "Failed to start call: " + err.message);
      startCallBtn.disabled = false;
      startCallBtn.style.display = "block";
      startCallBtn.querySelector("span").textContent = "Start Call";
      endCallBtn.style.display = "none";
      callInProgress = false;
    });
}

// ------------------------------------------------
// DTMF -- Send Digits
// ------------------------------------------------

function sendDTMF(digit) {
  if (!currentCallId || !callInProgress) return;

  var btn = document.querySelector('.dtmf-btn[data-digit="' + digit + '"]');
  if (btn) {
    btn.classList.add("pressed");
    setTimeout(function () { btn.classList.remove("pressed"); }, 200);
  }

  fetch("/api/send-dtmf/" + currentCallId, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ digits: digit }),
  })
    .then(function (resp) {
      if (!resp.ok) {
        return resp.json().then(function (body) {
          addMessage("error", "Error", "DTMF failed: " + (body.error || "Unknown"));
        });
      }
    })
    .catch(function (err) {
      console.error("DTMF error:", err);
    });
}

// Bind DTMF button clicks
document.querySelectorAll(".dtmf-btn").forEach(function (btn) {
  btn.addEventListener("click", function () {
    sendDTMF(this.getAttribute("data-digit"));
  });
});

// Keyboard shortcuts for DTMF during active call
document.addEventListener("keydown", function (e) {
  if (!callInProgress) return;
  // Don't capture when typing in an input/textarea
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

  var key = e.key;
  if ("0123456789*#".indexOf(key) !== -1) {
    e.preventDefault();
    sendDTMF(key);
  }
});

// ------------------------------------------------
// End Call
// ------------------------------------------------

function endCall() {
  if (!currentCallId || !callInProgress) return;

  endCallBtn.disabled = true;
  endCallBtn.querySelector("span").textContent = "Ending...";

  fetch("/api/end-call/" + currentCallId, { method: "POST" })
    .catch(function (err) {
      console.error("End call error:", err);
    });
}

// ------------------------------------------------
// Event Listeners
// ------------------------------------------------

sendBtn.addEventListener("click", sendText);
micBtn.addEventListener("click", toggleRecording);
startCallBtn.addEventListener("click", startCall);
if (endCallBtn) endCallBtn.addEventListener("click", endCall);

// Provider selector
document.querySelectorAll("input[name='provider']").forEach(function (radio) {
  radio.addEventListener("change", function () {
    selectedProvider = this.value;
    // Update call hint if it already has a target
    if (callHint && callHint.textContent.indexOf("Will call") === 0) {
      var provLabel = selectedProvider === "twilio" ? "Twilio" : "Exotel";
      callHint.textContent = callHint.textContent.replace(/ via (Twilio|Exotel)\./, " via " + provLabel + ".");
    }
  });
});


clearBtn.addEventListener("click", function () {
  conversation.innerHTML =
    '<div class="message system">' +
    '<div class="msg-label">System</div>' +
    '<div class="msg-text">Conversation cleared.</div>' +
    "</div>";
});

muteBtn.addEventListener("click", function () {
  audioMuted = !audioMuted;
  muteBtn.textContent = audioMuted ? "Unmute" : "Mute";
  muteBtn.classList.toggle("active", audioMuted);
});

textInput.addEventListener("keydown", function (e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendText();
  }
});

// ------------------------------------------------
// Contacts Panel
// ------------------------------------------------

var contactsList = $("#contactsList");
var contactsCount = $("#contactsCount");
var contactsBody = $("#contactsBody");
var contactsToggle = $("#contactsToggle");
var contactsChevron = $("#contactsChevron");
var addContactBtn = $("#addContactBtn");
var addNameInput = $("#addName");
var addPhoneInput = $("#addPhone");
var addCategorySelect = $("#addCategory");
var contactsExpanded = true;

var CATEGORY_COLORS = {
  hospital: "var(--accent2)",
  bank: "var(--accent3)",
  person: "var(--accent)",
  other: "var(--text-muted)",
};

function loadContacts() {
  fetch("/api/registry")
    .then(function (resp) { return resp.json(); })
    .then(function (contacts) {
      contactsCount.textContent = contacts.length;
      contactsList.innerHTML = "";
      if (contacts.length === 0) {
        contactsList.innerHTML =
          '<div class="contacts-empty">No contacts yet. Add one below.</div>';
        return;
      }
      contacts.forEach(function (c) {
        var color = CATEGORY_COLORS[c.category] || CATEGORY_COLORS.other;
        var div = document.createElement("div");
        div.className = "contact-card";
        div.innerHTML =
          '<div class="contact-info">' +
          '<span class="contact-cat" style="background:' + color + '">' +
          escapeHtml(c.category.charAt(0).toUpperCase()) + "</span>" +
          '<div class="contact-details">' +
          '<span class="contact-name">' + escapeHtml(c.name) + "</span>" +
          '<span class="contact-phone">' + escapeHtml(c.phone) + "</span>" +
          "</div></div>" +
          '<button class="contact-del" data-key="' + escapeHtml(c.key) +
          '" title="Delete">&times;</button>';
        contactsList.appendChild(div);
      });
      // Bind delete buttons
      contactsList.querySelectorAll(".contact-del").forEach(function (btn) {
        btn.addEventListener("click", function () {
          deleteContact(this.getAttribute("data-key"));
        });
      });
    })
    .catch(function (err) {
      console.error("Failed to load contacts:", err);
    });
}

function addContact() {
  var name = addNameInput.value.trim();
  var phone = addPhoneInput.value.trim();
  var category = addCategorySelect.value;
  if (!name || !phone) return;

  fetch("/api/registry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name, phone: phone, category: category }),
  })
    .then(function (resp) {
      if (!resp.ok) throw new Error("Failed to add contact");
      addNameInput.value = "";
      addPhoneInput.value = "";
      loadContacts();
    })
    .catch(function (err) {
      console.error("Add contact error:", err);
    });
}

function deleteContact(key) {
  fetch("/api/registry/" + encodeURIComponent(key), { method: "DELETE" })
    .then(function (resp) {
      if (!resp.ok) throw new Error("Failed to delete contact");
      loadContacts();
    })
    .catch(function (err) {
      console.error("Delete contact error:", err);
    });
}

contactsToggle.addEventListener("click", function () {
  contactsExpanded = !contactsExpanded;
  contactsBody.style.display = contactsExpanded ? "block" : "none";
  contactsChevron.style.transform = contactsExpanded ? "rotate(0)" : "rotate(-90deg)";
});

addContactBtn.addEventListener("click", addContact);

// Enter key on add inputs
addNameInput.addEventListener("keydown", function (e) {
  if (e.key === "Enter") { e.preventDefault(); addContact(); }
});
addPhoneInput.addEventListener("keydown", function (e) {
  if (e.key === "Enter") { e.preventDefault(); addContact(); }
});


// ------------------------------------------------
// Initialize
// ------------------------------------------------

loadContacts();
connect();
