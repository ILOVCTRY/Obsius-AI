(function (window, document) {
  "use strict";

  var stage = document.querySelector("[data-interactive-stage]");
  var engine = window.ConservatoryAudio;
  if (!stage || !engine) {
    return;
  }
  if (stage.classList.contains("zh-hero-r5")) {
    return;
  }

  var score = stage.querySelector("[data-living-score]");
  var notes = stage.querySelectorAll("[data-score-note]");
  var experienceButtons = stage.querySelectorAll("[data-score-experience]");
  var soundToggle = stage.querySelector("[data-sound-toggle]");
  var status = stage.querySelector("[data-score-status]");
  var pulse = stage.querySelector("[data-music-pulse]");
  var performanceAudio = stage.querySelector("[data-swan-lake-audio]");
  var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var finePointer = window.matchMedia && window.matchMedia("(pointer: fine)").matches;
  var playbackState = "idle";
  var muted = true;
  var timers = [];
  var sceneFrame = 0;
  var sceneRect = null;
  var scenePointerX = 0;
  var scenePointerY = 0;
  var totalDuration = 46;
  var phraseWow = 0.18;

  var phraseProfiles = {
    1: {
      phraseLight: 0.84,
      phraseShift: 0,
      phraseImageDepth: "-4px",
      phraseMediaTone: 0.04,
      phraseGoldGlow: "12px",
      phraseBreath: 0.008,
      phraseWow: 0.46,
      phraseClimax: 0.42,
      phraseTiltX: "-0.12deg",
      phraseTiltY: "0.08deg",
      lineIntensity: 0.56,
      foregroundShift: "1px"
    },
    2: {
      phraseLight: 0.98,
      phraseShift: 0,
      phraseImageDepth: "-8px",
      phraseMediaTone: 0.08,
      phraseGoldGlow: "18px",
      phraseBreath: 0.015,
      phraseWow: 0.92,
      phraseClimax: 0.92,
      phraseTiltX: "0.2deg",
      phraseTiltY: "-0.15deg",
      lineIntensity: 0.84,
      foregroundShift: "3px"
    },
    3: {
      phraseLight: 0.9,
      phraseShift: 0,
      phraseImageDepth: "-12px",
      phraseMediaTone: 0.07,
      phraseGoldGlow: "16px",
      phraseBreath: 0.011,
      phraseWow: 0.7,
      phraseClimax: 0.68,
      phraseTiltX: "-0.16deg",
      phraseTiltY: "0.09deg",
      lineIntensity: 0.74,
      foregroundShift: "5px"
    }
  };

  var swanLakeMotifBase = [
    { start: 0.00, duration: 0.72, pitch: 493.88, noteIndex: 0, visualTarget: "staff", intensity: 0.58, phrase: 1 },
    { start: 0.82, duration: 0.42, pitch: 554.37, noteIndex: 1, visualTarget: "note", intensity: 0.52, phrase: 1 },
    { start: 1.30, duration: 0.56, pitch: 587.33, noteIndex: 2, visualTarget: "note", intensity: 0.6, phrase: 1 },
    { start: 1.96, duration: 0.56, pitch: 659.25, noteIndex: 3, visualTarget: "lines", intensity: 0.64, phrase: 1 },
    { start: 2.64, duration: 1.18, pitch: 739.99, noteIndex: 4, visualTarget: "gold", intensity: 0.72, phrase: 1 },
    { start: 3.98, duration: 0.54, pitch: 587.33, noteIndex: 2, visualTarget: "note", intensity: 0.62, phrase: 1 },
    { start: 4.62, duration: 1.22, pitch: 739.99, noteIndex: 4, visualTarget: "copy", intensity: 0.74, phrase: 2 },
    { start: 6.02, duration: 0.68, pitch: 587.33, noteIndex: 2, visualTarget: "note", intensity: 0.6, phrase: 2 },
    { start: 6.82, duration: 1.42, pitch: 493.88, noteIndex: 0, visualTarget: "note", intensity: 0.7, phrase: 2 },
    { start: 8.44, duration: 0.70, pitch: 739.99, noteIndex: 4, visualTarget: "lines", intensity: 0.66, phrase: 2 },
    { start: 9.24, duration: 0.45, pitch: 659.25, noteIndex: 3, visualTarget: "note", intensity: 0.64, phrase: 2 },
    { start: 9.78, duration: 0.60, pitch: 587.33, noteIndex: 2, visualTarget: "copy", intensity: 0.59, phrase: 3 },
    { start: 10.48, duration: 0.60, pitch: 554.37, noteIndex: 1, visualTarget: "staff", intensity: 0.56, phrase: 3 },
    { start: 11.18, duration: 1.18, pitch: 493.88, noteIndex: 0, visualTarget: "note", intensity: 0.68, phrase: 3 },
    { start: 12.58, duration: 0.46, pitch: 466.16, noteIndex: 7, visualTarget: "gold", intensity: 0.54, phrase: 3 },
    { start: 13.14, duration: 0.56, pitch: 493.88, noteIndex: 0, visualTarget: "note", intensity: 0.61, phrase: 3 },
    { start: 13.82, duration: 0.66, pitch: 587.33, noteIndex: 2, visualTarget: "lines", intensity: 0.68, phrase: 3 },
    { start: 14.58, duration: 0.92, pitch: 739.99, noteIndex: 4, visualTarget: "copy", intensity: 0.78, phrase: 3 },
    { start: 15.64, duration: 0.62, pitch: 659.25, noteIndex: 3, visualTarget: "foreground", intensity: 0.69, phrase: 3 },
    { start: 16.38, duration: 0.68, pitch: 554.37, noteIndex: 1, visualTarget: "note", intensity: 0.6, phrase: 3 },
    { start: 17.18, duration: 1.78, pitch: 493.88, noteIndex: 0, visualTarget: "gold", intensity: 0.76, phrase: 3 }
  ];
  var swanLakeMotif = [];
  [0, 19.4, 38.8].forEach(function (offset, repeatIndex) {
    swanLakeMotifBase.forEach(function (event) {
      var start = event.start + offset;
      if (start >= totalDuration - 0.2) return;
      swanLakeMotif.push({
        start: start,
        duration: Math.min(event.duration, totalDuration - start),
        pitch: event.pitch,
        noteIndex: event.noteIndex,
        visualTarget: event.visualTarget,
        intensity: Math.min(0.96, event.intensity + repeatIndex * 0.06),
        phrase: ((event.phrase - 1 + repeatIndex) % 3) + 1
      });
    });
  });

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function normalizePhrase(value) {
    var phrase = parseInt(value, 10);
    if (!isFinite(phrase)) {
      return 1;
    }
    return clamp(phrase, 1, 3);
  }

  function addTimer(callback, delay) {
    var timer = window.setTimeout(callback, delay);
    timers.push(timer);
    return timer;
  }

  function clearTimers() {
    timers.forEach(function (timer) {
      window.clearTimeout(timer);
    });
    timers = [];
  }

  function setStatus(state) {
    if (!status) {
      return;
    }
    status.textContent = status.getAttribute("data-" + state) || "";
  }

  function updateExperienceButtons() {
    Array.prototype.forEach.call(experienceButtons, function (button) {
      var labelAttribute = playbackState === "playing" ? "data-label-playing" : (playbackState === "completed" || playbackState === "stopped") ? "data-label-replay" : "data-label-idle";
      var label = button.getAttribute(labelAttribute) || "Swan Lake";
      var copy = button.querySelector("strong");
      if (copy) {
        copy.textContent = label;
      }
      button.setAttribute("aria-label", label);
      button.setAttribute("aria-pressed", playbackState === "playing" ? "true" : "false");
    });
    stage.setAttribute("data-score-state", playbackState);
    document.body.classList.toggle("is-score-performing", playbackState === "playing");
    setStatus(playbackState);
  }

  function updateSoundToggle() {
    if (!soundToggle) {
      return;
    }
    var enabled = !muted;
    var label = enabled ? soundToggle.getAttribute("data-label-on") : soundToggle.getAttribute("data-label-off");
    var aria = enabled ? soundToggle.getAttribute("data-aria-on") : soundToggle.getAttribute("data-aria-off");
    soundToggle.textContent = label || "Sound";
    soundToggle.setAttribute("aria-label", aria || label || "Sound");
    soundToggle.setAttribute("aria-pressed", enabled ? "true" : "false");
  }

  function setMuted(nextMuted, fromGesture) {
    muted = Boolean(nextMuted);
    if (!muted && fromGesture) {
      engine.unlock();
    }
    engine.setMuted(muted);
    if (performanceAudio) {
      performanceAudio.muted = muted;
    }
    updateSoundToggle();
  }

  function pulseAt(origin) {
    if (!pulse || reducedMotion) {
      return;
    }
    pulse.style.setProperty("--pulse-origin", origin || "50%");
    pulse.classList.remove("is-pulsing");
    window.requestAnimationFrame(function () {
      pulse.classList.add("is-pulsing");
      addTimer(function () {
        pulse.classList.remove("is-pulsing");
      }, 720);
    });
  }

  function clearActiveNotes() {
    Array.prototype.forEach.call(notes, function (note) {
      note.classList.remove("is-sounding", "is-theme-note", "is-upcoming");
      note.style.removeProperty("--note-lift");
      note.style.removeProperty("--note-z");
      note.style.removeProperty("--note-glow");
    });
    stage.classList.remove("is-note-struck");
    stage.style.removeProperty("--note-impact-x");
    stage.style.removeProperty("--note-impact-intensity");
    stage.style.removeProperty("--note-ripple-intensity");
    stage.style.removeProperty("--score-wow");
    stage.removeAttribute("data-score-target");
    stage.removeAttribute("data-score-climax");
    return;
  }

  function activateNote(event, duration, fromTheme, targetHint) {
    var eventData = event && typeof event === "object" ? event : null;
    var index = Number(eventData ? eventData.noteIndex : event);
    if (!isFinite(index)) {
      return;
    }
    var note = notes[index % notes.length];
    if (!note) {
      return;
    }
    var intensity = clamp(Number(eventData ? eventData.intensity : null), 0.28, 1);
    var safeDuration = Number(duration);
    if (!isFinite(safeDuration) || safeDuration <= 0) {
      safeDuration = Number(eventData ? eventData.duration : 0.72);
    }
    var origin = note.style.getPropertyValue("--note-x") || "50%";
    var hint = String(eventData ? eventData.visualTarget || targetHint : targetHint || "note");
    var wow = clamp(0.26 + intensity * 0.45, 0.25, 1);
    note.classList.add("is-sounding");
    note.classList.remove("is-upcoming");
    note.classList.toggle("is-theme-note", Boolean(fromTheme));
    note.classList.toggle("is-lines-target", hint === "lines");
    note.classList.toggle("is-copy-target", hint === "copy");
    note.style.setProperty("--note-lift", (-(10 + intensity * 14)).toFixed(2) + "px");
    note.style.setProperty("--note-z", (14 + intensity * 28).toFixed(2) + "px");
    note.style.setProperty("--note-glow", Math.max(0.54, Math.min(1, 0.38 + intensity * 0.62)).toFixed(3));
    stage.style.setProperty("--note-impact-x", origin);
    stage.style.setProperty("--note-impact-intensity", clamp(intensity, 0.15, 1).toFixed(3));
    stage.style.setProperty("--note-ripple-intensity", clamp(0.22 + intensity * 0.64, 0.24, 1).toFixed(3));
    stage.style.setProperty("--score-wow", wow.toFixed(3));
    stage.setAttribute("data-score-target", hint);
    stage.classList.remove("is-note-struck");
    window.requestAnimationFrame(function () {
      stage.classList.add("is-note-struck");
    });
    pulseAt(origin);
    addTimer(function () {
      if (playbackState === "playing") {
        stage.style.setProperty("--score-wow", phraseWow.toFixed(3));
      }
      note.classList.remove("is-sounding", "is-theme-note", "is-upcoming");
      note.classList.remove("is-lines-target", "is-copy-target");
      note.style.removeProperty("--note-lift");
      note.style.removeProperty("--note-z");
      note.style.removeProperty("--note-glow");
      stage.classList.remove("is-note-struck");
    }, Math.max(420, Math.min(1050, safeDuration * 720)));
  }

  function primeNextNote(nextEvent, delayMs) {
    if (!nextEvent || delayMs <= 0) {
      return;
    }
    var nextIndex = Number(nextEvent.noteIndex);
    var nextNote = notes[nextIndex % notes.length];
    if (!nextNote) {
      return;
    }
    addTimer(function () {
      if (playbackState !== "playing") {
        return;
      }
      nextNote.classList.remove("is-sounding", "is-theme-note");
      nextNote.classList.add("is-upcoming");
      nextNote.style.setProperty("--note-lift", (-(4 + clamp(Number(nextEvent.intensity), 0.3, 0.95) * 3)).toFixed(2) + "px");
      nextNote.style.setProperty("--note-z", (4 + clamp(Number(nextEvent.intensity), 0.3, 0.95) * 8).toFixed(2) + "px");
      addTimer(function () {
      if (playbackState !== "playing") {
        return;
      }
      nextNote.classList.remove("is-upcoming");
      nextNote.style.removeProperty("--note-lift");
      nextNote.style.removeProperty("--note-z");
      if (playbackState === "playing") {
        stage.style.setProperty("--score-wow", phraseWow.toFixed(3));
      }
    }, Math.max(220, Math.min(680, Number(nextEvent.duration) * 560 + 140)));
  }, Math.max(20, Math.round(delayMs)));
  }

  function resetPhraseState() {
    stage.removeAttribute("data-score-phrase");
    stage.removeAttribute("data-score-target");
    stage.style.setProperty("--theme-progress", "0%");
    stage.style.setProperty("--phrase-light", "0.76");
    stage.style.setProperty("--phrase-shift", "0%");
    stage.style.setProperty("--phrase-image-depth", "0px");
    stage.style.setProperty("--phrase-media-tone", "0");
    stage.style.setProperty("--phrase-gold-glow", "0px");
    stage.style.setProperty("--phrase-breath-intensity", "0");
    stage.style.setProperty("--phrase-line-intensity", "0.22");
    stage.style.setProperty("--phrase-foreground-shift", "0px");
    stage.style.setProperty("--phrase-climax", "0");
    stage.style.setProperty("--score-wow", "0");
    stage.style.setProperty("--score-tilt-x", "0deg");
    stage.style.setProperty("--score-tilt-y", "0deg");
    stage.removeAttribute("data-score-climax");
    phraseWow = 0.18;
  }

  function applyPhrase(phrase, progress, intensity) {
    var phraseProfile = phraseProfiles[normalizePhrase(phrase)] || phraseProfiles[1];
    var phraseClimax = clamp(Number(phraseProfile.phraseClimax || 0.35), 0, 1);
    var phraseIntensity = clamp(Number(intensity || 0.4), 0.3, 1);
    var currentWow = clamp(phraseProfile.phraseWow * 0.64 + phraseIntensity * 0.36, 0.28, 1);
    var phraseClimaxState = phraseClimax >= 0.72 ? "high" : phraseClimax >= 0.46 ? "mid" : "low";
    stage.setAttribute("data-score-phrase", String(phrase));
    stage.setAttribute("data-score-climax", phraseClimaxState);
    stage.style.setProperty("--note-impact-intensity", clamp(progress, 0, 1).toFixed(3));
    stage.style.setProperty("--theme-progress", (progress * 100).toFixed(2) + "%");
    stage.style.setProperty("--phrase-light", phraseProfile.phraseLight.toFixed(3));
    stage.style.setProperty("--phrase-shift", (phraseProfile.phraseShift || 0) + "%");
    stage.style.setProperty("--phrase-image-depth", phraseProfile.phraseImageDepth);
    stage.style.setProperty("--phrase-media-tone", phraseProfile.phraseMediaTone.toFixed(3));
    stage.style.setProperty("--phrase-gold-glow", phraseProfile.phraseGoldGlow);
    stage.style.setProperty("--phrase-breath-intensity", phraseProfile.phraseBreath.toFixed(3));
    stage.style.setProperty("--phrase-line-intensity", phraseProfile.lineIntensity.toFixed(3));
    stage.style.setProperty("--phrase-foreground-shift", phraseProfile.foregroundShift || "0px");
    stage.style.setProperty("--phrase-climax", phraseClimax.toFixed(3));
    stage.style.setProperty("--score-wow", currentWow.toFixed(3));
    stage.style.setProperty("--score-tilt-x", phraseProfile.phraseTiltX || "0deg");
    stage.style.setProperty("--score-tilt-y", phraseProfile.phraseTiltY || "0deg");
    phraseWow = currentWow;
  }

  function playTimelineEvent(event) {
    if (playbackState !== "playing") {
      return;
    }
    if (event.phrase !== playTimelineEvent.activePhrase) {
      applyPhrase(normalizePhrase(event.phrase), event.start / totalDuration, event.intensity);
      playTimelineEvent.activePhrase = event.phrase;
    }
    if (!muted && !performanceAudio) {
      var velocity = clamp(0.35 + Number(event.intensity) * 0.62, 0.28, 0.96);
      engine.playNote(event.pitch, event.duration, velocity);
    }
    activateNote(event, event.duration, true, event.visualTarget);
  }

  playTimelineEvent.activePhrase = null;

  function finishPlayback(completed) {
    clearTimers();
    engine.stopAll();
    if (performanceAudio) {
      performanceAudio.pause();
      performanceAudio.currentTime = 0;
    }
    clearActiveNotes();
    resetPhraseState();
    stage.style.removeProperty("--score-wow");
    playbackState = completed ? "completed" : "stopped";
    updateExperienceButtons();
    stage.style.setProperty("--theme-progress", completed ? "100%" : "0%");
    stage.dispatchEvent(new CustomEvent("conservatory:scoreend"));
  }

  function stopPlayback() {
    if (playbackState !== "playing") {
      return;
    }
    finishPlayback(false);
  }

  function startPlayback() {
    clearTimers();
    engine.stopAll();
    function beginTimeline() {
      playbackState = "playing";
      resetPhraseState();
      stage.style.setProperty("--score-wow", "0.2");
      updateExperienceButtons();
      stage.style.setProperty("--theme-progress", "0%");
      playTimelineEvent.activePhrase = null;
      stage.dispatchEvent(new CustomEvent("conservatory:scoreplay"));

      swanLakeMotif.forEach(function (event) {
        addTimer(function () {
          playTimelineEvent(event);
        }, Math.round(event.start * 1000));
      });

      swanLakeMotif.forEach(function (event, index) {
        var nextEvent = swanLakeMotif[index + 1];
        if (!nextEvent) return;
        primeNextNote(nextEvent, Math.round((nextEvent.start - event.start - 0.12) * 1000));
      });

      addTimer(function () {
        if (playbackState === "playing") finishPlayback(true);
      }, Math.round(totalDuration * 1000));
    }

    setMuted(false, false);
    if (performanceAudio) {
      performanceAudio.currentTime = 0;
      performanceAudio.volume = 0.82;
      performanceAudio.play().then(beginTimeline).catch(function () {
        performanceAudio = null;
        engine.unlock().then(beginTimeline);
      });
      return;
    }
    engine.unlock().then(beginTimeline);
  }

  Array.prototype.forEach.call(experienceButtons, function (button) {
    button.addEventListener("click", function () {
      if (playbackState === "playing") {
        stopPlayback();
      } else {
        startPlayback();
      }
    });
  });

  if (soundToggle) {
    soundToggle.addEventListener("click", function () {
      setMuted(!muted, true);
    });
  }

  Array.prototype.forEach.call(notes, function (note) {
    note.addEventListener("click", function () {
      var frequency = Number(note.getAttribute("data-frequency"));
      var index = Number(note.getAttribute("data-note-index"));
      if (!muted) {
        engine.playNote(frequency, 0.68, 0.64);
      }
      activateNote(index, 0.68, false);
    });
  });

  function renderConductor() {
    if (!sceneRect) {
      sceneFrame = 0;
      return;
    }
    var x = Math.max(-1, Math.min(1, (scenePointerX - sceneRect.left) / sceneRect.width * 2 - 1));
    var y = Math.max(-1, Math.min(1, (scenePointerY - sceneRect.top) / sceneRect.height * 2 - 1));
    var atmosphere = 1 - (Math.abs(x) * 0.15 + Math.abs(y) * 0.15);
    stage.style.setProperty("--depth-bg-x", (x * 3).toFixed(2) + "px");
    stage.style.setProperty("--depth-bg-y", (y * 3).toFixed(2) + "px");
    stage.style.setProperty("--depth-staff-x", (x * 5).toFixed(2) + "px");
    stage.style.setProperty("--depth-staff-y", (y * 5).toFixed(2) + "px");
    stage.style.setProperty("--depth-note-x", (x * 6).toFixed(2) + "px");
    stage.style.setProperty("--depth-note-y", (y * 4).toFixed(2) + "px");
    stage.style.setProperty("--depth-media-x", (x * 8).toFixed(2) + "px");
    stage.style.setProperty("--depth-media-y", (y * 8).toFixed(2) + "px");
    stage.style.setProperty("--depth-copy-x", (x * -10).toFixed(2) + "px");
    stage.style.setProperty("--depth-copy-y", (y * -10).toFixed(2) + "px");
    stage.style.setProperty("--depth-front-x", (x * 16).toFixed(2) + "px");
    stage.style.setProperty("--depth-front-y", (y * 16).toFixed(2) + "px");
    stage.style.setProperty("--depth-light-x", (x * 14).toFixed(2) + "px");
    stage.style.setProperty("--depth-light-y", (y * 12).toFixed(2) + "px");
    stage.style.setProperty("--conductor-light-x", (50 + x * 26).toFixed(1) + "%");
    stage.style.setProperty("--conductor-light-y", (38 + y * 14).toFixed(1) + "%");
    stage.style.setProperty("--conductor-light-shift", (x * -12).toFixed(1) + "%");
    stage.style.setProperty("--stage-rotate-x", (y * -1.2).toFixed(2) + "deg");
    stage.style.setProperty("--stage-rotate-y", (x * 1.2).toFixed(2) + "deg");
    stage.style.setProperty("--depth-light-intensity", (0.72 + atmosphere * 0.07).toFixed(3));
    sceneFrame = 0;
  }

  function resetConductor() {
    sceneRect = null;
    ["--depth-bg-x", "--depth-bg-y", "--depth-staff-x", "--depth-staff-y", "--depth-note-x", "--depth-note-y", "--depth-media-x", "--depth-media-y", "--depth-copy-x", "--depth-copy-y", "--depth-front-x", "--depth-front-y", "--depth-light-x", "--depth-light-y", "--depth-light-intensity", "--conductor-light-x", "--conductor-light-y", "--conductor-light-shift", "--stage-rotate-x", "--stage-rotate-y"].forEach(function (property) {
      stage.style.removeProperty(property);
    });
  }

  if (!reducedMotion && finePointer && window.matchMedia("(min-width: 53rem)").matches) {
    if (!stage.classList.contains("zh-hero-r5")) {
      stage.addEventListener("pointerenter", function () {
        sceneRect = stage.getBoundingClientRect();
      });
      stage.addEventListener("pointermove", function (event) {
        scenePointerX = event.clientX;
        scenePointerY = event.clientY;
        if (!sceneFrame) {
          sceneFrame = window.requestAnimationFrame(renderConductor);
        }
      });
      stage.addEventListener("pointerleave", resetConductor);
    }

    Array.prototype.forEach.call(document.querySelectorAll("[data-depth-card]:not(.latest-feature)"), function (card) {
      var cardRect = null;
      var cardFrame = 0;
      var cardX = 0;
      var cardY = 0;
      card.addEventListener("pointerenter", function () {
        cardRect = card.getBoundingClientRect();
      });
      card.addEventListener("pointermove", function (event) {
        cardX = event.clientX;
        cardY = event.clientY;
        if (!cardFrame) {
          cardFrame = window.requestAnimationFrame(function () {
            if (cardRect) {
              var rx = Math.max(-1.5, Math.min(1.5, (cardY - cardRect.top) / cardRect.height * -3 + 1.5));
              var ry = Math.max(-2, Math.min(2, (cardX - cardRect.left) / cardRect.width * 4 - 2));
              card.style.setProperty("--card-rx", rx.toFixed(2) + "deg");
              card.style.setProperty("--card-ry", ry.toFixed(2) + "deg");
            }
            cardFrame = 0;
          });
        }
      });
      card.addEventListener("pointerleave", function () {
        cardRect = null;
        card.style.removeProperty("--card-rx");
        card.style.removeProperty("--card-ry");
      });
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopPlayback();
    }
  });
  window.addEventListener("pagehide", function () {
    clearTimers();
    engine.stopAll();
    if (performanceAudio) performanceAudio.pause();
  }, { once: true });

  setMuted(true, false);
  updateExperienceButtons();
})(window, document);

/* Virtual Conductor: one damped controller drives the accepted six-plane Hero. */
(function () {
  "use strict";

  var stage = document.querySelector(".interactive-stage.zh-hero-r5");
  var finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var desktopScene = window.matchMedia("(min-width: 53rem)");
  var frame = 0;
  var lastTime = 0;
  var bounds = null;
  var active = false;
  var targetX = 0;
  var targetY = 0;
  var currentX = 0;
  var currentY = 0;
  var lightX = 0;
  var lightY = 0;
  var resizeObserver = null;

  if (!stage) {
    return;
  }

  var layers = {
    background: stage.querySelector(".zh-hero-r5-backdrop"),
    atmosphere: stage.querySelector(".stage-atmosphere-r4"),
    staff: stage.querySelector(".living-score"),
    visual: stage.querySelector(".headline-stage-slide.is-active .stage-media"),
    typography: stage.querySelector(".headline-stage-slide.is-active .stage-copy"),
    foreground: stage.querySelector(".stage-foreground-r4")
  };

  if (!layers.background || !layers.atmosphere || !layers.staff || !layers.visual || !layers.typography || !layers.foreground) {
    return;
  }

  if (document.documentElement.classList.contains("legacy-browser") || !desktopScene.matches || !finePointer.matches || reduceMotion.matches) {
    stage.dataset.spatialReady = "static";
    stage.dataset.spatialPointer = "rest";
    return;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function damping(rate, delta) {
    return 1 - Math.exp(-rate * delta);
  }

  function setSceneVariables(x, y, slowX, slowY) {
    stage.style.setProperty("--pointer-depth-x", x.toFixed(4));
    stage.style.setProperty("--pointer-depth-y", y.toFixed(4));
    stage.style.setProperty("--pointer-light-x", slowX.toFixed(4));
    stage.style.setProperty("--pointer-light-y", slowY.toFixed(4));
    stage.style.setProperty("--pointer-depth-energy", Math.max(Math.abs(x), Math.abs(y)).toFixed(4));
  }

  function render(time) {
    var delta = lastTime ? Math.min(40, time - lastTime) : 16.67;
    var sceneRate = active ? .0072 : .0038;
    var sceneDamping = damping(sceneRate, delta);
    var lightDamping = damping(active ? .0028 : .0022, delta);
    var sceneDistance;
    var lightDistance;

    lastTime = time;
    currentX += (targetX - currentX) * sceneDamping;
    currentY += (targetY - currentY) * sceneDamping;
    lightX += (targetX - lightX) * lightDamping;
    lightY += (targetY - lightY) * lightDamping;
    setSceneVariables(currentX, currentY, lightX, lightY);

    sceneDistance = Math.max(Math.abs(targetX - currentX), Math.abs(targetY - currentY));
    lightDistance = Math.max(Math.abs(targetX - lightX), Math.abs(targetY - lightY));

    if (sceneDistance > .0006 || lightDistance > .0006) {
      frame = window.requestAnimationFrame(render);
      return;
    }

    currentX = targetX;
    currentY = targetY;
    lightX = targetX;
    lightY = targetY;
    setSceneVariables(currentX, currentY, lightX, lightY);
    frame = 0;
    lastTime = 0;
    if (!active) {
      stage.dataset.spatialPointer = "rest";
    }
  }

  function requestRender() {
    if (!frame) {
      frame = window.requestAnimationFrame(render);
    }
  }

  function syncBounds() {
    bounds = stage.getBoundingClientRect();
  }

  function onPointerEnter() {
    if (!desktopScene.matches || !finePointer.matches || reduceMotion.matches) {
      return;
    }
    active = true;
    syncBounds();
    stage.dataset.spatialPointer = "active";
  }

  function onPointerMove(event) {
    if (!active || !bounds || !desktopScene.matches || !finePointer.matches || reduceMotion.matches || event.pointerType === "touch") {
      return;
    }

    targetX = clamp(((event.clientX - bounds.left) / bounds.width) * 2 - 1, -1, 1);
    targetY = clamp(((event.clientY - bounds.top) / bounds.height) * 2 - 1, -1, 1);
    requestRender();
  }

  function resetScene() {
    active = false;
    bounds = null;
    targetX = 0;
    targetY = 0;
    stage.dataset.spatialPointer = "returning";
    requestRender();
  }

  function onPreferenceChange() {
    if (reduceMotion.matches || !finePointer.matches || !desktopScene.matches) {
      resetScene();
    }
  }

  stage.dataset.spatialReady = "true";
  stage.dataset.spatialPointer = "rest";
  stage.addEventListener("pointerenter", onPointerEnter, { passive: true });
  stage.addEventListener("pointermove", onPointerMove, { passive: true });
  stage.addEventListener("pointerleave", resetScene, { passive: true });
  stage.addEventListener("pointercancel", resetScene, { passive: true });
  reduceMotion.addEventListener("change", onPreferenceChange);
  finePointer.addEventListener("change", onPreferenceChange);
  desktopScene.addEventListener("change", onPreferenceChange);
  setSceneVariables(0, 0, 0, 0);

  if ("ResizeObserver" in window) {
    resizeObserver = new ResizeObserver(function () {
      if (bounds) {
        syncBounds();
      }
    });
    resizeObserver.observe(stage);
  }

  window.addEventListener("pagehide", function () {
    if (frame) {
      window.cancelAnimationFrame(frame);
    }
    if (resizeObserver) {
      resizeObserver.disconnect();
    }
  }, { once: true });
}());

/* Swan Lake Signature Timeline. One source drives scheduled audio and visual notes. */
(function () {
  "use strict";

  var stage = document.querySelector(".interactive-stage.zh-hero-r5");
  var engine = window.ConservatoryAudio;
  if (!stage || !engine) return;

  var buttons = Array.prototype.slice.call(stage.querySelectorAll("[data-score-experience]"));
  var soundButton = stage.querySelector("[data-sound-toggle]");
  var status = stage.querySelector("[data-score-status]");
  var performanceAudio = stage.querySelector("[data-swan-lake-audio]");
  var notes = Array.prototype.slice.call(stage.querySelectorAll("[data-score-note]"));
  var playbackState = "idle";
  var soundEnabled = false;
  var timelineFrame = 0;
  var audioStartTime = 0;
  var cursor = 0;
  var currentPhrase = 0;
  var upcomingNote = null;
  var carouselWasPaused = false;
  var sessionToken = 0;
  var schedulerRunning = false;
  var usingTrack = false;
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var totalDuration = 46;
  var trajectoryPaths = Array.prototype.slice.call(stage.querySelectorAll("[data-swan-trajectory-path]"));
  var fineConductor = window.matchMedia("(min-width: 53rem) and (hover: hover) and (pointer: fine)");
  var conductor = {
    enabled: Boolean(fineConductor.matches && !reducedMotion.matches),
    targetX: 0,
    targetY: 0,
    currentX: 0,
    currentY: 0,
    proximity: 0,
    rect: null
  };

  var beatDuration = 60 / 64;
  var visualTargets = ["staff", "note", "lines", "gold", "copy", "note", "foreground", "gold"];
  var pitchByMidi = { 71: 493.88, 73: 554.37, 74: 587.33, 76: 659.25, 78: 739.99, 79: 783.99, 81: 880, 83: 987.77 };
  var noteIndexByMidi = { 71: 0, 73: 1, 74: 2, 76: 3, 78: 4, 79: 5, 81: 6, 83: 0 };
  var themePhrases = [
    [[71, 1.5], [73, .5], [74, 1], [76, 1], [78, 2], [74, 1], [78, 1], [71, 2]],
    [[71, 1.5], [73, .5], [74, 1], [76, 1], [78, 1], [81, 1], [78, 1], [74, 1], [71, 2]],
    [[78, 1], [79, .5], [81, .5], [83, 2], [81, 1], [78, 1], [76, 1], [74, 1], [73, 1], [71, 1]],
    [[71, 1], [73, 1], [74, 1], [78, 1], [83, 2], [81, 1], [78, 1], [74, 1], [71, 1]],
    [[76, 1], [78, 1], [79, 1], [78, 1], [74, 1], [73, 1], [71, 2]]
  ];
  var sequenceBeat = .28;
  var SWAN_LAKE_SEQUENCE = [];
  themePhrases.forEach(function (phrase, phraseIndex) {
    phrase.forEach(function (note, noteIndex) {
      var midi = note[0];
      var beats = note[1];
      SWAN_LAKE_SEQUENCE.push({
        start: sequenceBeat * beatDuration,
        duration: beats * beatDuration * 1.05,
        pitch: pitchByMidi[midi],
        visualTarget: visualTargets[(noteIndex + phraseIndex) % visualTargets.length],
        intensity: Math.min(.96, .58 + phraseIndex * .075 + (noteIndex % 4) * .045),
        phrase: phraseIndex + 1,
        noteIndex: noteIndexByMidi[midi]
      });
      sequenceBeat += beats;
    });
  });

  var PHRASE_TIMELINE = [
    { phrase: 1, start: 0, end: 9.64, light: .32, imageZ: -7, headlineZ: 9, gold: .035, breath: .007, scoreZ: 58, scoreY: -2, foregroundY: -2, trajectoryStrength: .42, trajectory: "M20 310 C180 340 315 286 470 300 C620 314 760 274 980 236" },
    { phrase: 2, start: 9.64, end: 19.01, light: .44, imageZ: -11, headlineZ: 13, gold: .065, breath: .011, scoreZ: 70, scoreY: -4, foregroundY: -4, trajectoryStrength: .58, trajectory: "M18 305 C210 265 330 150 505 186 C680 220 790 250 982 118" },
    { phrase: 3, start: 19.01, end: 28.39, light: .62, imageZ: -17, headlineZ: 19, gold: .115, breath: .019, scoreZ: 90, scoreY: -8, foregroundY: -8, trajectoryStrength: .84, trajectory: "M20 282 C210 360 360 104 545 142 C700 172 780 72 982 172" },
    { phrase: 4, start: 28.39, end: 37.76, light: .68, imageZ: -19, headlineZ: 21, gold: .13, breath: .021, scoreZ: 94, scoreY: -9, foregroundY: -9, trajectoryStrength: .9, trajectory: "M22 274 C204 336 346 86 548 132 C714 168 806 62 984 158" },
    { phrase: 5, start: 37.76, end: 46, light: .5, imageZ: -13, headlineZ: 15, gold: .085, breath: .012, scoreZ: 78, scoreY: -5, foregroundY: -5, trajectoryStrength: .66, trajectory: "M980 268 C760 214 650 300 500 212 C340 118 206 152 64 80" }
  ];

  function clamp(value, min, max) { return Math.min(max, Math.max(min, value)); }

  function labelForState(state, button) {
    if (state === "playing") return button.getAttribute("data-label-playing") || "停止演奏";
    if (state === "completed" || state === "stopped") return button.getAttribute("data-label-replay") || "再次体验";
    if (state === "starting") return "正在准备";
    return button.getAttribute("data-label-idle") || "体验 · 天鹅湖";
  }

  function setState(nextState) {
    playbackState = nextState;
    stage.dataset.signatureState = nextState;
    stage.dataset.scoreState = nextState;
    document.body.classList.toggle("is-score-performing", nextState === "playing");
    buttons.forEach(function (button) {
      var label = labelForState(nextState, button);
      button.setAttribute("aria-pressed", nextState === "playing" ? "true" : "false");
      button.setAttribute("aria-busy", nextState === "starting" || nextState === "stopping" ? "true" : "false");
      button.setAttribute("aria-label", label);
      var strong = button.querySelector(".experience-copy strong");
      if (strong) strong.textContent = label;
    });
    if (status) {
      status.textContent = nextState === "playing"
        ? (status.getAttribute("data-playing") || "正在演奏天鹅湖主题短动机")
        : nextState === "completed"
          ? (status.getAttribute("data-completed") || "演奏完成，可再次体验")
          : nextState === "stopped"
            ? (status.getAttribute("data-stopped") || "演奏已停止")
            : (status.getAttribute("data-idle") || "天鹅湖互动乐谱已就绪");
    }
  }

  function syncSoundButton() {
    if (!soundButton) return;
    var label = soundEnabled ? soundButton.getAttribute("data-label-on") : soundButton.getAttribute("data-label-off");
    var aria = soundEnabled ? soundButton.getAttribute("data-aria-on") : soundButton.getAttribute("data-aria-off");
    soundButton.textContent = label || (soundEnabled ? "静音" : "开启声音");
    soundButton.setAttribute("aria-label", aria || label);
    soundButton.setAttribute("aria-pressed", soundEnabled ? "true" : "false");
  }

  function setSoundEnabled(nextEnabled) {
    soundEnabled = Boolean(nextEnabled);
    engine.setMuted(!soundEnabled);
    if (performanceAudio) {
      performanceAudio.muted = !soundEnabled;
    }
    syncSoundButton();
  }

  function setConductorTarget(event) {
    if (!conductor.enabled) return;
    if (!conductor.rect) conductor.rect = stage.getBoundingClientRect();
    var rect = conductor.rect;
    conductor.targetX = clamp(((event.clientX - rect.left) / rect.width) * 2 - 1, -1, 1);
    conductor.targetY = clamp(((event.clientY - rect.top) / rect.height) * 2 - 1, -1, 1);
  }

  function resetConductorTarget() {
    conductor.targetX = 0;
    conductor.targetY = 0;
    conductor.rect = null;
  }

  function updateConductor(activeEvent) {
    var damping = conductor.enabled ? .075 : .12;
    conductor.currentX += (conductor.targetX - conductor.currentX) * damping;
    conductor.currentY += (conductor.targetY - conductor.currentY) * damping;
    var proximity = 0;
    if (conductor.enabled && activeEvent) {
      var note = notes[activeEvent.noteIndex % notes.length];
      var noteX = parseFloat(note.style.getPropertyValue("--note-x")) / 100 * 2 - 1;
      var noteY = parseFloat(note.style.getPropertyValue("--note-y")) / 100 * 2 - 1;
      var distance = Math.hypot(conductor.currentX - noteX, conductor.currentY - noteY);
      proximity = clamp(1 - distance / .58, 0, 1);
    }
    conductor.proximity += (proximity - conductor.proximity) * .1;
    var verticalExpression = clamp(-conductor.currentY, -1, 1);
    var expression = clamp(1 + verticalExpression * .16 + conductor.proximity * .14, .8, 1.26);
    var harmonyExpression = clamp(.64 + verticalExpression * .1 + conductor.proximity * .18, .52, .92);
    stage.style.setProperty("--conductor-x", (conductor.currentX * 6).toFixed(2) + "px");
    stage.style.setProperty("--conductor-y", (conductor.currentY * 3).toFixed(2) + "px");
    stage.style.setProperty("--conductor-note-x", (conductor.currentX * 4.8).toFixed(2) + "px");
    stage.style.setProperty("--conductor-note-y", (-verticalExpression * 7 - conductor.proximity * 4).toFixed(2) + "px");
    stage.style.setProperty("--conductor-note-z", (verticalExpression * 15 + conductor.proximity * 13).toFixed(2) + "px");
    stage.style.setProperty("--conductor-note-scale", clamp(.97 + (expression - .8) * .22, .97, 1.07).toFixed(3));
    stage.style.setProperty("--conductor-glow", (8 + Math.max(0, verticalExpression) * 7 + conductor.proximity * 7).toFixed(2) + "px");
    stage.style.setProperty("--conductor-light-x", (conductor.currentX * 12).toFixed(2) + "px");
    stage.style.setProperty("--conductor-light-strength", clamp(.94 + (expression - .8) * .28, .94, 1.07).toFixed(3));
    stage.style.setProperty("--conductor-harmony-opacity", harmonyExpression.toFixed(3));
    stage.style.setProperty("--conductor-harmony-scale", clamp(1.02 + conductor.proximity * .1 + Math.max(0, verticalExpression) * .04, 1.02, 1.16).toFixed(3));
    stage.style.setProperty("--conductor-ripple-x", (conductor.currentX * 6).toFixed(2) + "px");
    stage.style.setProperty("--conductor-expression", expression.toFixed(3));
    stage.style.setProperty("--conductor-proximity", conductor.proximity.toFixed(3));
    stage.dataset.conductorActive = conductor.enabled && playbackState === "playing" ? "true" : "false";
    stage.dataset.conductorProximity = conductor.proximity > .42 ? "near" : "rest";
    stage.dataset.conductorDirection = conductor.currentX < -.18 ? "left" : conductor.currentX > .18 ? "right" : "center";
    return expression;
  }

  function resetPerformanceVisuals() {
    stage.dataset.conductorActive = "false";
    stage.dataset.conductorProximity = "rest";
    stage.dataset.conductorDirection = "center";
    stage.style.setProperty("--conductor-x", "0px");
    stage.style.setProperty("--conductor-y", "0px");
    stage.style.setProperty("--conductor-note-x", "0px");
    stage.style.setProperty("--conductor-note-y", "0px");
    stage.style.setProperty("--conductor-note-z", "0px");
    stage.style.setProperty("--conductor-note-scale", "1");
    stage.style.setProperty("--conductor-glow", "8px");
    stage.style.setProperty("--conductor-light-x", "0px");
    stage.style.setProperty("--conductor-light-strength", "1");
    stage.style.setProperty("--conductor-harmony-opacity", ".64");
    stage.style.setProperty("--conductor-harmony-scale", "1.02");
    stage.style.setProperty("--conductor-ripple-x", "0px");
    stage.style.setProperty("--conductor-expression", "1");
    stage.style.setProperty("--conductor-proximity", "0");
    stage.style.setProperty("--trajectory-progress", "0");
    stage.style.setProperty("--trajectory-opacity", "0");
    stage.style.setProperty("--trajectory-front-opacity", "0");
    conductor.currentX = 0;
    conductor.currentY = 0;
    conductor.proximity = 0;
    resetConductorTarget();
  }

  function clearNoteClasses() {
    notes.forEach(function (note) {
      note.classList.remove("is-signature-current", "is-signature-upcoming", "is-signature-resonance");
      note.style.removeProperty("--signature-note-lift");
      note.style.removeProperty("--signature-note-z");
      note.style.removeProperty("--signature-note-scale");
      delete note.dataset.signatureToken;
    });
    upcomingNote = null;
    stage.classList.remove("is-signature-ripple", "is-signature-wing-sweep");
    delete stage.dataset.signatureTarget;
  }

  function resetStageVariables() {
    stage.style.setProperty("--signature-image-z", "0px");
    stage.style.setProperty("--signature-headline-z", "0px");
    stage.style.setProperty("--signature-score-z", "-46px");
    stage.style.setProperty("--signature-score-y", "0px");
    stage.style.setProperty("--signature-foreground-y", "0px");
    stage.style.setProperty("--signature-backdrop-scale", "1.14");
    stage.style.setProperty("--signature-light-scale", "1.08");
    stage.style.setProperty("--signature-light-opacity", ".8");
    stage.style.setProperty("--signature-gold", "0");
    stage.style.setProperty("--signature-progress", "0");
    stage.style.setProperty("--performance-beat", "0");
    delete stage.dataset.signaturePhrase;
    currentPhrase = 0;
  }

  function pulseStaff() {
    if (reducedMotion.matches) return;
    stage.classList.remove("is-signature-ripple");
    void stage.offsetWidth;
    stage.classList.add("is-signature-ripple");
  }

  function activateVisualEvent(event, eventIndex) {
    var note = notes[event.noteIndex % notes.length];
    var token = String(eventIndex) + ":" + String(event.start) + ":" + String(sessionToken);
    notes.forEach(function (candidate) { candidate.classList.remove("is-signature-current"); });
    if (upcomingNote === note) {
      note.classList.remove("is-signature-upcoming");
      upcomingNote = null;
    }
    note.dataset.signatureToken = token;
    note.style.setProperty("--signature-note-lift", (-12 - event.intensity * 15).toFixed(1) + "px");
    note.style.setProperty("--signature-note-z", (42 + event.intensity * 42).toFixed(1) + "px");
    note.style.setProperty("--signature-note-scale", (1.12 + event.intensity * .22).toFixed(3));
    note.classList.add("is-signature-current");
    var resonanceDirection = conductor.enabled && conductor.currentX < -.15 ? -1 : 1;
    var resonanceNote = notes[(event.noteIndex + resonanceDirection + notes.length) % notes.length];
    if (resonanceNote && resonanceNote !== note && !reducedMotion.matches) {
      var resonanceToken = token + ":resonance";
      resonanceNote.dataset.signatureResonanceToken = resonanceToken;
      resonanceNote.classList.add("is-signature-resonance");
      window.setTimeout(function () {
        if (resonanceNote.dataset.signatureResonanceToken === resonanceToken) {
          resonanceNote.classList.remove("is-signature-resonance");
          delete resonanceNote.dataset.signatureResonanceToken;
        }
      }, Math.min(720, event.duration * 720));
    }
    stage.dataset.signatureTarget = event.visualTarget;
    pulseStaff();
    window.setTimeout(function () {
      if (note.dataset.signatureToken === token) note.classList.remove("is-signature-current");
    }, event.duration * 1000);
  }

  function updateUpcoming(elapsed) {
    var nextEvent = SWAN_LAKE_SEQUENCE[cursor];
    var nextNote = nextEvent && notes[nextEvent.noteIndex % notes.length];
    var shouldPrime = nextEvent && nextEvent.start - elapsed <= .42 && nextEvent.start > elapsed;
    if (!shouldPrime) {
      if (upcomingNote) upcomingNote.classList.remove("is-signature-upcoming");
      upcomingNote = null;
      return;
    }
    if (upcomingNote !== nextNote) {
      if (upcomingNote) upcomingNote.classList.remove("is-signature-upcoming");
      upcomingNote = nextNote;
      if (!nextNote.classList.contains("is-signature-current")) nextNote.classList.add("is-signature-upcoming");
    }
  }

  function updatePhraseTimeline(elapsed) {
    var profile = PHRASE_TIMELINE[PHRASE_TIMELINE.length - 1];
    for (var i = 0; i < PHRASE_TIMELINE.length; i += 1) {
      if (elapsed >= PHRASE_TIMELINE[i].start && elapsed < PHRASE_TIMELINE[i].end) { profile = PHRASE_TIMELINE[i]; break; }
    }
    var localProgress = clamp((elapsed - profile.start) / (profile.end - profile.start), 0, 1);
    var arc = Math.sin(localProgress * Math.PI);
    var beatPhase = elapsed / beatDuration;
    var beatPulse = reducedMotion.matches ? 0 : Math.pow(Math.max(0, Math.cos((beatPhase % 1) * Math.PI * 2)), 8);
    if (currentPhrase !== profile.phrase) {
      var previousPhrase = currentPhrase;
      currentPhrase = profile.phrase;
      stage.dataset.signaturePhrase = String(profile.phrase);
      if (previousPhrase && !reducedMotion.matches) {
        stage.classList.remove("is-signature-wing-sweep");
        void stage.offsetWidth;
        stage.classList.add("is-signature-wing-sweep");
      }
      trajectoryPaths.forEach(function (path) { path.setAttribute("d", profile.trajectory); });
    }
    if (elapsed - profile.start >= 1.05) stage.classList.remove("is-signature-wing-sweep");
    stage.style.setProperty("--signature-image-z", (profile.imageZ - arc * 3).toFixed(2) + "px");
    stage.style.setProperty("--signature-headline-z", (profile.headlineZ + arc * 3).toFixed(2) + "px");
    stage.style.setProperty("--signature-score-z", (profile.scoreZ + arc * 8).toFixed(2) + "px");
    stage.style.setProperty("--signature-score-y", (profile.scoreY - arc * 2 - beatPulse * .7).toFixed(2) + "px");
    stage.style.setProperty("--signature-foreground-y", (profile.foregroundY - arc * 2).toFixed(2) + "px");
    stage.style.setProperty("--signature-backdrop-scale", (1.14 + profile.breath + arc * .006 + beatPulse * .0015).toFixed(4));
    stage.style.setProperty("--signature-light-scale", (1.08 + profile.light * .07 + arc * .018 + beatPulse * .006).toFixed(4));
    stage.style.setProperty("--signature-light-opacity", (.76 + profile.light * .16 + arc * .02).toFixed(3));
    stage.style.setProperty("--signature-gold", (profile.gold + arc * .02).toFixed(3));
    stage.style.setProperty("--signature-progress", clamp(elapsed / totalDuration, 0, 1).toFixed(4));
    stage.style.setProperty("--performance-beat", beatPulse.toFixed(4));
    var trajectoryRise = clamp(localProgress / .14, 0, 1);
    var trajectoryFade = clamp((1 - localProgress) / .22, 0, 1);
    var trajectoryEnvelope = trajectoryRise * trajectoryFade;
    var crossingEnvelope = Math.sin(clamp((localProgress - .08) / .78, 0, 1) * Math.PI);
    stage.style.setProperty("--trajectory-progress", localProgress.toFixed(4));
    stage.style.setProperty("--trajectory-opacity", (trajectoryEnvelope * profile.trajectoryStrength * .44).toFixed(3));
    stage.style.setProperty("--trajectory-front-opacity", (crossingEnvelope * profile.trajectoryStrength * .32).toFixed(3));
  }

  function pauseCarouselForPerformance() {
    var pause = stage.querySelector("[data-carousel-pause]");
    carouselWasPaused = false;
    if (pause && pause.getAttribute("aria-pressed") !== "true") { pause.click(); carouselWasPaused = true; }
  }

  function resumeCarouselAfterPerformance() {
    var pause = stage.querySelector("[data-carousel-pause]");
    if (carouselWasPaused && pause && pause.getAttribute("aria-pressed") === "true") pause.click();
    carouselWasPaused = false;
  }

  function stopPlayback() {
    if (playbackState !== "playing" && playbackState !== "starting") return;
    setState("stopping");
    sessionToken += 1;
    schedulerRunning = false;
    if (timelineFrame) window.cancelAnimationFrame(timelineFrame);
    timelineFrame = 0;
    engine.stopAll();
    if (performanceAudio) {
      performanceAudio.pause();
      performanceAudio.currentTime = 0;
    }
    usingTrack = false;
    clearNoteClasses();
    resetPerformanceVisuals();
    resetStageVariables();
    resumeCarouselAfterPerformance();
    setState("stopped");
  }

  function finishPlayback() {
    schedulerRunning = false;
    if (timelineFrame) window.cancelAnimationFrame(timelineFrame);
    timelineFrame = 0;
    engine.stopAll();
    if (performanceAudio) {
      performanceAudio.pause();
      performanceAudio.currentTime = 0;
    }
    usingTrack = false;
    clearNoteClasses();
    resetPerformanceVisuals();
    resetStageVariables();
    setState("completed");
    resumeCarouselAfterPerformance();
  }

  function renderTimeline() {
    var audio = engine.getDiagnostics();
    if (playbackState !== "playing") { timelineFrame = 0; return; }
    if (!usingTrack && audio.state !== "running") { timelineFrame = 0; return; }
    var elapsed = usingTrack && performanceAudio ? performanceAudio.currentTime : audio.currentTime - audioStartTime;
    while (cursor < SWAN_LAKE_SEQUENCE.length && elapsed >= SWAN_LAKE_SEQUENCE[cursor].start) {
      activateVisualEvent(SWAN_LAKE_SEQUENCE[cursor], cursor);
      cursor += 1;
    }
    updateUpcoming(elapsed);
    updatePhraseTimeline(elapsed);
    var activeEvent = null;
    for (var eventIndex = 0; eventIndex < SWAN_LAKE_SEQUENCE.length; eventIndex += 1) {
      var candidate = SWAN_LAKE_SEQUENCE[eventIndex];
      if (elapsed >= candidate.start && elapsed <= candidate.start + candidate.duration) { activeEvent = candidate; break; }
    }
    updateConductor(activeEvent);
    if (elapsed >= totalDuration) { finishPlayback(); return; }
    timelineFrame = window.requestAnimationFrame(renderTimeline);
  }

  function scheduleSequence() {
    var scheduled = SWAN_LAKE_SEQUENCE.map(function (event) {
      return engine.scheduleNote(event.pitch, event.duration, event.intensity * .72, audioStartTime + event.start);
    });
    if (scheduled.some(function (voice) { return !voice; })) throw new Error("Swan Lake scheduler could not create every voice");
    schedulerRunning = true;
  }

  function startPlayback() {
    if (!notes.length || playbackState === "playing" || playbackState === "starting") return;
    var token = ++sessionToken;
    setState("starting");
    engine.stopAll();
    function beginTimeline() {
      if (token !== sessionToken) return;
      setSoundEnabled(true);
      clearNoteClasses();
      resetPerformanceVisuals();
      resetStageVariables();
      cursor = 0;
      pauseCarouselForPerformance();
      setState("playing");
      updatePhraseTimeline(0);
      timelineFrame = window.requestAnimationFrame(renderTimeline);
    }
    function startFallback() {
      usingTrack = false;
      engine.unlock().then(function (context) {
        if (token !== sessionToken) return;
        if (!context || context.state !== "running") throw new Error("AudioContext is not running");
        audioStartTime = context.currentTime + .06;
        setSoundEnabled(true);
        scheduleSequence();
        beginTimeline();
      }).catch(failPlayback);
    }
    function failPlayback(error) {
      if (token !== sessionToken) return;
      schedulerRunning = false;
      engine.stopAll();
      setSoundEnabled(false);
      clearNoteClasses();
      resetStageVariables();
      setState("error");
      window.console.error("[Swan Lake] Audio start failed", error);
    }

    if (!performanceAudio) {
      startFallback();
      return;
    }

    performanceAudio.currentTime = 0;
    performanceAudio.volume = 0.84;
    performanceAudio.muted = false;
    performanceAudio.play().then(function () {
      if (token !== sessionToken) return;
      usingTrack = true;
      schedulerRunning = true;
      beginTimeline();
    }).catch(function (error) {
      window.console.warn("[Swan Lake] Local excerpt unavailable; using synthesized fallback", error);
      startFallback();
    });
  }

  function playInteractiveNote(note, index) {
    var frequency = Number(note.getAttribute("data-frequency"));
    engine.unlock().then(function (context) {
      if (!context || context.state !== "running") return;
      setSoundEnabled(true);
      if (!engine.playNote(frequency, .58, .62)) throw new Error("Playable note voice was not created");
      activateVisualEvent({ start: 0, duration: .58, pitch: frequency, visualTarget: "note", intensity: .66, phrase: currentPhrase || 1, noteIndex: index }, "tap-" + index);
    }).catch(function (error) {
      window.console.error("[Swan Lake] Playable note failed", error);
    });
  }

  buttons.forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (playbackState === "playing" || playbackState === "starting") stopPlayback();
      else startPlayback();
    }, true);
  });

  if (conductor.enabled) {
    stage.addEventListener("pointerenter", function () { conductor.rect = stage.getBoundingClientRect(); }, { passive: true });
    stage.addEventListener("pointermove", setConductorTarget, { passive: true });
    stage.addEventListener("pointerleave", resetConductorTarget, { passive: true });
  }

  notes.forEach(function (note, index) {
    note.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopImmediatePropagation();
      playInteractiveNote(note, index);
    }, true);
  });

  if (soundButton) {
    soundButton.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (soundEnabled) { setSoundEnabled(false); return; }
      engine.unlock().then(function (context) {
        if (context && context.state === "running") setSoundEnabled(true);
      }).catch(function (error) {
        setSoundEnabled(false);
        window.console.error("[Swan Lake] Audio unlock failed", error);
      });
    }, true);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden && (playbackState === "playing" || playbackState === "starting")) stopPlayback();
  });

  window.addEventListener("pagehide", function () {
    sessionToken += 1;
    schedulerRunning = false;
    if (timelineFrame) window.cancelAnimationFrame(timelineFrame);
    engine.stopAll();
    if (performanceAudio) performanceAudio.pause();
  }, { once: true });

  setSoundEnabled(false);
  resetPerformanceVisuals();
  resetStageVariables();
  setState("idle");

  window.SwanLakeSignature = {
    timeline: SWAN_LAKE_SEQUENCE,
    phraseTimeline: PHRASE_TIMELINE,
    duration: totalDuration,
    play: startPlayback,
    stop: stopPlayback,
    getState: function () {
      return { playbackState: playbackState, schedulerRunning: schedulerRunning, soundEnabled: soundEnabled, usingTrack: usingTrack, trackTime: performanceAudio ? performanceAudio.currentTime : 0, cursor: cursor, conductor: { x: conductor.currentX, y: conductor.currentY, proximity: conductor.proximity }, audio: engine.getDiagnostics() };
    }
  };
}());

/* Unified secondary pointer and native-scroll spatial controller. */
(() => {
  const root = document.documentElement;
  const hero = document.querySelector('.zh-hero-r5');
  const feature = document.querySelector('.latest-feature[data-depth-card]');
  const sections = Array.from(document.querySelectorAll('main > section'));
  const finePointer = window.matchMedia('(min-width: 53rem) and (hover: hover) and (pointer: fine)');
  const scrollAllowed = window.matchMedia('(min-width: 53rem) and (prefers-reduced-motion: no-preference)');

  if (root.classList.contains('legacy-browser')) return;

  const hasScrollScene = hero && sections.length >= 6 && scrollAllowed.matches;
  const hasFeaturePointer = feature && finePointer.matches && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!hasScrollScene && !hasFeaturePointer) return;

  const markerSections = hasScrollScene ? sections.slice(1, 6) : [];
  const performance = hasScrollScene ? sections[4] : null;
  const gallery = hasScrollScene ? sections[5] : null;
  const state = {
    scroll: window.scrollY,
    frame: 0,
    featureBounds: null,
    pointer: { x: 0, y: 0, targetX: 0, targetY: 0 },
    current: {
      staff: 0,
      foreground: 0,
      performanceLight: 0,
      galleryBack: 0,
      markers: markerSections.map(() => 0)
    },
    target: {
      staff: 0,
      foreground: 0,
      performanceLight: 0,
      galleryBack: 0,
      markers: markerSections.map(() => 0)
    }
  };

  let metrics = null;
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const approach = (current, target, damping) => current + (target - current) * damping;

  const measure = () => {
    state.featureBounds = hasFeaturePointer ? feature.getBoundingClientRect() : null;
    if (!hasScrollScene) return;
    const pageY = window.scrollY;
    metrics = {
      viewport: window.innerHeight,
      heroHeight: hero.getBoundingClientRect().height,
      sections: sections.map((section) => {
        const bounds = section.getBoundingClientRect();
        return { top: bounds.top + pageY, height: bounds.height };
      })
    };
  };

  const sectionPhase = (index) => {
    const metric = metrics.sections[index];
    const viewportCenter = state.scroll + metrics.viewport * 0.5;
    const sectionCenter = metric.top + metric.height * 0.5;
    return clamp((viewportCenter - sectionCenter) / ((metrics.viewport + metric.height) * 0.5), -1, 1);
  };

  const updateScrollTargets = () => {
    if (!hasScrollScene || !metrics) return;
    const heroExit = clamp(state.scroll / Math.max(metrics.heroHeight, 1), 0, 1);
    state.target.staff = heroExit * 14;
    state.target.foreground = heroExit * -10;
    state.target.performanceLight = sectionPhase(4) * 8;
    state.target.galleryBack = sectionPhase(5) * 12;
    state.target.markers = markerSections.map((_, index) => sectionPhase(index + 1) * -9);
  };

  const render = () => {
    let unsettled = false;

    if (hasFeaturePointer) {
      state.pointer.x = approach(state.pointer.x, state.pointer.targetX, 0.095);
      state.pointer.y = approach(state.pointer.y, state.pointer.targetY, 0.095);
      feature.style.setProperty('--card-rx', `${(-state.pointer.y * 1.8).toFixed(3)}deg`);
      feature.style.setProperty('--card-ry', `${(state.pointer.x * 2).toFixed(3)}deg`);
      if (Math.abs(state.pointer.targetX - state.pointer.x) > 0.001 || Math.abs(state.pointer.targetY - state.pointer.y) > 0.001) unsettled = true;
    }

    if (hasScrollScene) {
      updateScrollTargets();
      const settle = (key) => {
        state.current[key] = approach(state.current[key], state.target[key], 0.14);
        if (Math.abs(state.current[key] - state.target[key]) > 0.02) unsettled = true;
      };

      settle('staff');
      settle('foreground');
      settle('performanceLight');
      settle('galleryBack');
      state.current.markers = state.current.markers.map((value, index) => {
        const next = approach(value, state.target.markers[index], 0.14);
        if (Math.abs(next - state.target.markers[index]) > 0.02) unsettled = true;
        return next;
      });

      hero.style.setProperty('--scroll-staff-y', `${state.current.staff.toFixed(2)}px`);
      hero.style.setProperty('--scroll-foreground-y', `${state.current.foreground.toFixed(2)}px`);
      performance.style.setProperty('--scroll-performance-light-y', `${state.current.performanceLight.toFixed(2)}px`);
      gallery.style.setProperty('--scroll-gallery-back-y', `${state.current.galleryBack.toFixed(2)}px`);
      markerSections.forEach((section, index) => {
        section.style.setProperty('--scroll-marker-y', `${state.current.markers[index].toFixed(2)}px`);
      });
    }

    state.frame = unsettled ? requestAnimationFrame(render) : 0;
  };

  const requestRender = () => {
    if (!state.frame) state.frame = requestAnimationFrame(render);
  };

  if (hasFeaturePointer) {
    feature.addEventListener('pointerenter', () => {
      state.featureBounds = feature.getBoundingClientRect();
    }, { passive: true });
    feature.addEventListener('pointermove', (event) => {
      const bounds = state.featureBounds || feature.getBoundingClientRect();
      state.pointer.targetX = clamp(((event.clientX - bounds.left) / bounds.width) * 2 - 1, -1, 1);
      state.pointer.targetY = clamp(((event.clientY - bounds.top) / bounds.height) * 2 - 1, -1, 1);
      requestRender();
    }, { passive: true });
    feature.addEventListener('pointerleave', () => {
      state.pointer.targetX = 0;
      state.pointer.targetY = 0;
      state.featureBounds = null;
      requestRender();
    }, { passive: true });
  }

  if (hasScrollScene) {
    window.addEventListener('scroll', () => {
      state.scroll = window.scrollY;
      requestRender();
    }, { passive: true });
  }

  window.addEventListener('resize', () => {
    measure();
    state.scroll = window.scrollY;
    requestRender();
  }, { passive: true });

  measure();
  requestRender();
})();

(function () {
  "use strict";

  var stage = document.querySelector(".interactive-stage.zh-hero-r5");
  if (!stage || typeof MutationObserver === "undefined") return;

  var notesLayer = stage.querySelector(".playable-score-notes");
  var identity = stage.querySelector(".stage-identity");
  if (!notesLayer || !identity) return;

  var svgNS = "http://www.w3.org/2000/svg";
  var wings = document.createElementNS(svgNS, "svg");
  wings.setAttribute("class", "coda-brand-wings");
  wings.setAttribute("viewBox", "0 0 220 72");
  wings.setAttribute("preserveAspectRatio", "xMidYMid meet");
  wings.setAttribute("aria-hidden", "true");
  wings.setAttribute("focusable", "false");

  var leftWing = document.createElementNS(svgNS, "path");
  leftWing.setAttribute("d", "M110 50 C88 19 47 13 8 39 C42 28 74 33 110 50");

  var rightWing = document.createElementNS(svgNS, "path");
  rightWing.setAttribute("d", "M110 50 C132 19 173 13 212 39 C178 28 146 33 110 50");

  wings.appendChild(leftWing);
  wings.appendChild(rightWing);
  stage.appendChild(wings);

  var resolveTimer = 0;
  var resetTimer = 0;
  var lastState = stage.getAttribute("data-score-state") || "idle";
  var lastPhrase = stage.getAttribute("data-signature-phrase") || "";
  var lastActiveNote = null;

  function removeFinalNote() {
    if (!lastActiveNote) return;
    lastActiveNote.classList.remove("is-coda-final-release");
    lastActiveNote = null;
  }

  function clearResolve() {
    window.clearTimeout(resolveTimer);
    window.clearTimeout(resetTimer);
    resolveTimer = 0;
    resetTimer = 0;
    if (stage.classList.contains("is-coda-resolving") || stage.classList.contains("is-coda-graceful-reset")) {
      stage.classList.remove("is-coda-resolving", "is-coda-graceful-reset");
    }
    if (stage.hasAttribute("data-coda-resolve")) stage.removeAttribute("data-coda-resolve");
    removeFinalNote();
  }

  function beginResolve() {
    var finalNote = lastActiveNote;
    clearResolve();
    lastActiveNote = finalNote;
    stage.setAttribute("data-coda-resolve", "resolving");
    stage.classList.add("is-coda-resolving");

    if (lastActiveNote) {
      lastActiveNote.classList.add("is-coda-final-release");
    }

    resolveTimer = window.setTimeout(function () {
      stage.classList.remove("is-coda-resolving");
      stage.removeAttribute("data-coda-resolve");
      removeFinalNote();
      resolveTimer = 0;
    }, 1750);
  }

  function gracefulStop() {
    clearResolve();
    stage.classList.add("is-coda-graceful-reset");
    resetTimer = window.setTimeout(function () {
      stage.classList.remove("is-coda-graceful-reset");
      resetTimer = 0;
    }, 420);
  }

  var stateObserver = new MutationObserver(function () {
    var state = stage.getAttribute("data-score-state") || "idle";
    var phrase = stage.getAttribute("data-signature-phrase") || "";

    if (phrase) lastPhrase = phrase;
    if (state === lastState) return;

    if (state === "playing" && lastState !== "playing") {
      clearResolve();
    } else if (state === "completed" && lastState === "playing" && lastPhrase === "5") {
      beginResolve();
    } else if (state === "stopped") {
      if (lastPhrase === "5") gracefulStop();
      else clearResolve();
    } else if (state === "idle") {
      clearResolve();
    }

    lastState = state;
  });

  var noteObserver = new MutationObserver(function () {
    var active = notesLayer.querySelector(".score-note.is-signature-current");
    if (active) lastActiveNote = active;
  });

  stateObserver.observe(stage, {
    attributes: true,
    attributeFilter: ["data-score-state", "data-signature-phrase"]
  });

  noteObserver.observe(notesLayer, {
    attributes: true,
    subtree: true,
    attributeFilter: ["class"]
  });
})();

/* Fine-pointer signature: a pointed swan-note body is the hotspot; its neck follows. */
(function () {
  "use strict";

  var stage = document.querySelector("[data-interactive-stage]");
  var finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (!stage || !finePointer.matches || reducedMotion.matches) return;

  var cursor = document.createElement("span");
  var currentX = -40;
  var currentY = -40;
  var targetX = -40;
  var targetY = -40;
  var previousPointerX = null;
  var previousPointerY = null;
  var noteOffsetX = 0;
  var noteOffsetY = 0;
  var noteVelocityX = 0;
  var noteVelocityY = 0;
  var noteTilt = 0;
  var noteTiltTarget = 0;
  var cursorSpeed = 0;
  var cursorSpeedTarget = 0;
  var hoveredControl = null;
  var movingTimer = 0;
  var frame = 0;
  cursor.className = "music-note-cursor";
  cursor.setAttribute("aria-hidden", "true");
  cursor.innerHTML = [
    '<span class="music-note-cursor__focus"></span>',
    '<span class="music-note-cursor__symbol">',
    '<span class="music-note-cursor__mark"></span>',
    '<svg class="music-note-cursor__ribbon" viewBox="0 0 36 18" focusable="false" aria-hidden="true">',
    '<path d="M35 5.5C25.5 1.5 16.5 12.5 1 7.5"></path>',
    '<path d="M34 10C24 6 16 16 3 12.2"></path>',
    '</svg>',
    '</span>',
    '<span class="music-note-cursor__echo">♫</span>'
  ].join("");
  document.body.appendChild(cursor);
  document.documentElement.classList.add("has-music-note-cursor");

  function renderCursor() {
    /* Keep the brand swan's pointed body on the pointer; only its ribbon carries inertia. */
    currentX = targetX;
    currentY = targetY;
    cursor.style.transform = "translate3d(" + currentX.toFixed(2) + "px," + currentY.toFixed(2) + "px,0)";

    noteVelocityX += -noteOffsetX * .12;
    noteVelocityY += -noteOffsetY * .12;
    noteVelocityX *= .76;
    noteVelocityY *= .76;
    noteOffsetX += noteVelocityX;
    noteOffsetY += noteVelocityY;
    noteTilt += (noteTiltTarget - noteTilt) * .16;
    noteTiltTarget *= .84;
    cursorSpeed += (cursorSpeedTarget - cursorSpeed) * .18;
    cursorSpeedTarget *= .8;

    cursor.style.setProperty("--cursor-tilt", (noteTilt * .72).toFixed(2) + "deg");
    cursor.style.setProperty("--cursor-note-x", (noteOffsetX * .34).toFixed(2) + "px");
    cursor.style.setProperty("--cursor-note-y", (noteOffsetY * .28).toFixed(2) + "px");
    cursor.style.setProperty("--cursor-speed", cursorSpeed.toFixed(3));

    var motionEnergy = Math.abs(noteOffsetX) + Math.abs(noteOffsetY) + Math.abs(noteVelocityX) + Math.abs(noteVelocityY) + Math.abs(noteTilt) + cursorSpeed;
    if (motionEnergy > .025) {
      frame = window.requestAnimationFrame(renderCursor);
    } else {
      noteOffsetX = 0;
      noteOffsetY = 0;
      noteVelocityX = 0;
      noteVelocityY = 0;
      noteTilt = 0;
      cursorSpeed = 0;
      cursor.style.setProperty("--cursor-tilt", "0deg");
      cursor.style.setProperty("--cursor-note-x", "0px");
      cursor.style.setProperty("--cursor-note-y", "0px");
      cursor.style.setProperty("--cursor-speed", "0");
      frame = 0;
    }
  }

  function requestRender() {
    if (!frame) frame = window.requestAnimationFrame(renderCursor);
  }

  document.addEventListener("pointermove", function (event) {
    if (event.pointerType === "touch") return;
    var deltaX = previousPointerX === null ? 0 : event.clientX - previousPointerX;
    var deltaY = previousPointerY === null ? 0 : event.clientY - previousPointerY;
    var speed = Math.min(1, Math.hypot(deltaX, deltaY) / 22);
    previousPointerX = event.clientX;
    previousPointerY = event.clientY;
    targetX = event.clientX;
    targetY = event.clientY;
    noteVelocityX = Math.max(-3.4, Math.min(3.4, noteVelocityX - deltaX * .048));
    noteVelocityY = Math.max(-2.7, Math.min(2.7, noteVelocityY - deltaY * .042));
    noteTiltTarget = Math.max(-9, Math.min(9, deltaX * .3));
    cursorSpeedTarget = Math.max(cursorSpeedTarget, speed);
    cursor.classList.add("is-visible");
    cursor.classList.add("is-moving");
    window.clearTimeout(movingTimer);
    movingTimer = window.setTimeout(function () {
      cursor.classList.remove("is-moving");
    }, 110);
    requestRender();
  }, { passive: true });

  document.addEventListener("pointerover", function (event) {
    var interactive = event.target.closest("a, button, [role='button'], [data-score-note]");
    var control = event.target.closest("button, .button, [role='button']");
    cursor.classList.toggle("is-interactive", Boolean(interactive));
    if (hoveredControl !== control) {
      if (hoveredControl) hoveredControl.classList.remove("is-music-cursor-hover");
      hoveredControl = control;
      if (hoveredControl) hoveredControl.classList.add("is-music-cursor-hover");
    }
  }, { passive: true });

  document.addEventListener("pointerdown", function (event) {
    if (event.pointerType !== "touch") cursor.classList.add("is-pressed");
  }, { passive: true });

  document.addEventListener("pointerup", function () {
    cursor.classList.remove("is-pressed");
  }, { passive: true });

  document.documentElement.addEventListener("mouseleave", function () {
    cursor.classList.remove("is-visible", "is-interactive", "is-moving", "is-pressed");
    if (hoveredControl) hoveredControl.classList.remove("is-music-cursor-hover");
    hoveredControl = null;
    previousPointerX = null;
    previousPointerY = null;
    noteOffsetX = 0;
    noteOffsetY = 0;
    noteVelocityX = 0;
    noteVelocityY = 0;
    noteTilt = 0;
    noteTiltTarget = 0;
    cursorSpeed = 0;
    cursorSpeedTarget = 0;
  });

  window.addEventListener("pagehide", function () {
    if (frame) window.cancelAnimationFrame(frame);
    window.clearTimeout(movingTimer);
    if (hoveredControl) hoveredControl.classList.remove("is-music-cursor-hover");
  }, { once: true });
})();
