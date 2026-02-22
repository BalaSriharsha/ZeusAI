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
var userNameInput = $("#userName");
var userPhoneInput = $("#userPhone");
var callHint = $("#callHint");

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
  startCallBtn.disabled = false;
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
  startCallBtn.querySelector("span").textContent = "Start Call";
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
        audio.play().catch(function () {});
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
  startCallBtn.querySelector("span").textContent = "Calling...";
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
      startCallBtn.querySelector("span").textContent = "Start Call";
      callInProgress = false;
    });
}

// ------------------------------------------------
// Event Listeners
// ------------------------------------------------

sendBtn.addEventListener("click", sendText);
micBtn.addEventListener("click", toggleRecording);
startCallBtn.addEventListener("click", startCall);

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
// Initialize
// ------------------------------------------------

connect();
