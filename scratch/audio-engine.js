(function (window) {
  "use strict";

  var audioContext = null;
  var masterGain = null;
  var toneFilter = null;
  var analyser = null;
  var masterGainConnected = false;
  var voices = [];
  var muted = true;
  var maximumInteractiveVoices = 6;
  var audibleGain = 0.22;

  function createGraph() {
    var Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return null;
    audioContext = new Context();
    masterGain = audioContext.createGain();
    toneFilter = audioContext.createBiquadFilter();
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.35;
    toneFilter.type = "lowpass";
    toneFilter.frequency.setValueAtTime(4600, audioContext.currentTime);
    toneFilter.Q.setValueAtTime(0.62, audioContext.currentTime);
    toneFilter.connect(masterGain);
    masterGain.connect(audioContext.destination);
    masterGain.connect(analyser);
    masterGainConnected = true;
    masterGain.gain.setValueAtTime(0.0001, audioContext.currentTime);
    return audioContext;
  }

  function getContext() {
    if (!audioContext || audioContext.state === "closed") return createGraph();
    return audioContext;
  }

  function unlock() {
    var context;
    try {
      context = getContext();
    } catch (error) {
      return Promise.reject(error);
    }
    if (!context) return Promise.reject(new Error("Web Audio API is unavailable"));
    if (context.state === "running") return Promise.resolve(context);
    return context.resume().then(function () {
      if (context.state !== "running") throw new Error("AudioContext did not enter the running state");
      return context;
    });
  }

  function setMuted(nextMuted) {
    muted = Boolean(nextMuted);
    if (!audioContext || !masterGain || audioContext.state === "closed") return;
    var now = audioContext.currentTime;
    masterGain.gain.cancelScheduledValues(now);
    masterGain.gain.setTargetAtTime(muted ? 0.0001 : audibleGain, now, 0.022);
  }

  function removeVoice(voice) {
    var index = voices.indexOf(voice);
    if (index !== -1) voices.splice(index, 1);
    voice.nodes.forEach(function (node) {
      try { node.disconnect(); } catch (error) { return; }
    });
  }

  function stopVoice(voice, release) {
    if (!voice || voice.stopped || !audioContext) return;
    voice.stopped = true;
    var now = audioContext.currentTime;
    var releaseTime = Math.max(0.025, release || 0.06);
    voice.gain.gain.cancelScheduledValues(now);
    voice.gain.gain.setTargetAtTime(0.0001, now, releaseTime / 3);
    voice.oscillators.forEach(function (oscillator) {
      try { oscillator.stop(now + releaseTime + 0.04); } catch (error) { return; }
    });
  }

  function stopAll() {
    voices.slice().forEach(function (voice) { stopVoice(voice, 0.055); });
  }

  function pruneInteractiveVoices() {
    var interactive = voices.filter(function (voice) { return !voice.ended && !voice.sequence; });
    while (interactive.length >= maximumInteractiveVoices) stopVoice(interactive.shift(), 0.03);
  }

  function createVoice(frequency, duration, velocity, startTime, sequence) {
    var context = getContext();
    if (!context || context.state !== "running" || muted || !frequency) return null;
    var now = context.currentTime;
    var startsAt = Math.max(now + 0.006, Number(startTime) || now + 0.006);
    var noteLength = Math.max(0.24, Math.min(1.8, duration || 0.7));
    var noteVelocity = Math.max(0.22, Math.min(1, velocity || 0.72));
    var voiceGain = context.createGain();
    var partials = [
      { ratio: 1, level: 0.72, type: "sine" },
      { ratio: 2, level: 0.2, type: "sine" },
      { ratio: 3, level: 0.07, type: "sine" },
      { ratio: 4.01, level: 0.025, type: "triangle" }
    ];
    var voice = { gain: voiceGain, oscillators: [], nodes: [voiceGain], stopped: false, ended: false, sequence: Boolean(sequence) };

    voiceGain.gain.setValueAtTime(0.0001, startsAt);
    voiceGain.gain.exponentialRampToValueAtTime(noteVelocity * 0.24, startsAt + 0.032);
    voiceGain.gain.exponentialRampToValueAtTime(noteVelocity * 0.095, startsAt + Math.min(0.26, noteLength * 0.38));
    voiceGain.gain.exponentialRampToValueAtTime(0.0001, startsAt + noteLength);
    voiceGain.connect(toneFilter);

    partials.forEach(function (partial, index) {
      var oscillator = context.createOscillator();
      var partialGain = context.createGain();
      oscillator.type = partial.type;
      oscillator.frequency.setValueAtTime(frequency * partial.ratio, startsAt);
      oscillator.detune.setValueAtTime(index === 3 ? -5 : index === 2 ? 2 : 0, startsAt);
      partialGain.gain.setValueAtTime(partial.level, startsAt);
      oscillator.connect(partialGain);
      partialGain.connect(voiceGain);
      oscillator.start(startsAt);
      oscillator.stop(startsAt + noteLength + 0.08);
      voice.oscillators.push(oscillator);
      voice.nodes.push(oscillator, partialGain);
    });

    voice.oscillators[0].addEventListener("ended", function () {
      voice.ended = true;
      removeVoice(voice);
    }, { once: true });
    voices.push(voice);
    return voice;
  }

  function playNote(frequency, duration, velocity) {
    pruneInteractiveVoices();
    return createVoice(frequency, duration, velocity, null, false);
  }

  function scheduleNote(frequency, duration, velocity, startTime) {
    return createVoice(frequency, duration, velocity, startTime, true);
  }

  function playTestNote() {
    return unlock().then(function (context) {
      setMuted(false);
      if (!playNote(659.25, 0.46, 0.58)) throw new Error("Audio smoke test voice was not created");
      return context;
    });
  }

  function signalLevel() {
    if (!analyser) return 0;
    var samples = new Float32Array(analyser.fftSize);
    var sum = 0;
    analyser.getFloatTimeDomainData(samples);
    samples.forEach(function (sample) { sum += sample * sample; });
    return Math.sqrt(sum / samples.length);
  }

  function diagnostics() {
    return {
      available: Boolean(window.AudioContext || window.webkitAudioContext),
      created: Boolean(audioContext),
      state: audioContext ? audioContext.state : "uninitialized",
      currentTime: audioContext ? audioContext.currentTime : 0,
      muted: muted,
      masterGainConnected: masterGainConnected,
      masterGainValue: masterGain ? masterGain.gain.value : 0,
      activeVoices: voices.filter(function (voice) { return !voice.ended; }).length,
      signalLevel: signalLevel()
    };
  }

  window.ConservatoryAudio = {
    isAvailable: function () { return Boolean(window.AudioContext || window.webkitAudioContext); },
    unlock: unlock,
    playNote: playNote,
    scheduleNote: scheduleNote,
    playTestNote: playTestNote,
    setMuted: setMuted,
    isMuted: function () { return muted; },
    stopAll: stopAll,
    activeVoiceCount: function () { return diagnostics().activeVoices; },
    getContextState: function () { return diagnostics().state; },
    getDiagnostics: diagnostics
  };
})(window);