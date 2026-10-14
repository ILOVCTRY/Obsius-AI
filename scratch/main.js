"use strict";

document.documentElement.classList.add("js");

(function () {
  var body = document.body;
  var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var menuToggle = document.querySelector("[data-menu-toggle]");
  var primaryNav = document.querySelector("[data-primary-nav]");
  var mobileNavigation = window.matchMedia ? window.matchMedia("(max-width: 1220px)") : null;
  var submenuToggles = document.querySelectorAll("[data-submenu-toggle]");
  var backToTop = document.querySelector("[data-back-to-top]");
  var header = document.querySelector("[data-header]");
  var hero = document.querySelector("[data-hero]");
  var lightbox = document.querySelector("[data-lightbox]");
  var lastLightboxTrigger = null;
  var lightboxItems = [];
  var lightboxIndex = 0;
  var touchStartX = 0;

  function setExpanded(button, expanded) {
    if (button) {
      button.setAttribute("aria-expanded", expanded ? "true" : "false");
    }
  }

  function closeSubmenus(exceptButton) {
    Array.prototype.forEach.call(submenuToggles, function (button) {
      if (button !== exceptButton) {
        setExpanded(button, false);
        if (button.closest(".nav-item")) {
          button.closest(".nav-item").classList.remove("is-open");
        }
      }
    });
  }

  function closeMenu(restoreFocus) {
    if (!menuToggle || !primaryNav) {
      return;
    }
    primaryNav.classList.remove("is-open");
    body.classList.remove("nav-open");
    setExpanded(menuToggle, false);
    closeSubmenus();
    if (restoreFocus) {
      menuToggle.focus();
    }
  }

  if (menuToggle && primaryNav) {
    menuToggle.addEventListener("click", function () {
      var open = !primaryNav.classList.contains("is-open");
      primaryNav.classList.toggle("is-open", open);
      body.classList.toggle("nav-open", open);
      setExpanded(menuToggle, open);
    });
  }

  if (mobileNavigation) {
    var resetNavigationAtDesktop = function (event) {
      if (!event.matches) {
        closeMenu(false);
      }
    };
    if (mobileNavigation.addEventListener) {
      mobileNavigation.addEventListener("change", resetNavigationAtDesktop);
    } else if (mobileNavigation.addListener) {
      mobileNavigation.addListener(resetNavigationAtDesktop);
    }
  }

  Array.prototype.forEach.call(submenuToggles, function (button) {
    button.addEventListener("click", function () {
      var item = button.closest(".nav-item");
      var open = item && !item.classList.contains("is-open");
      closeSubmenus(button);
      if (item) {
        item.classList.toggle("is-open", open);
      }
      setExpanded(button, open);
    });
    button.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        button.click();
      }
    });
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-primary-nav]") || event.target.closest("[data-menu-toggle]")) {
      return;
    }
    if (primaryNav && primaryNav.classList.contains("is-open")) {
      closeMenu(false);
    } else {
      closeSubmenus();
    }
  });

  function initCarouselsLegacy() {
    var carousels = document.querySelectorAll("[data-carousel]");
    Array.prototype.forEach.call(carousels, function (carousel) {
      var slides = carousel.querySelectorAll("[data-slide]");
      var prev = carousel.querySelector("[data-carousel-prev]");
      var next = carousel.querySelector("[data-carousel-next]");
      var dots = carousel.querySelectorAll("[data-carousel-dot]");
      var counter = carousel.querySelector("[data-carousel-counter]");
      var index = 0;
      var timer = 0;
      var carouselTouchStartX = 0;

      if (!slides.length) {
        return;
      }

      function show(newIndex) {
        index = (newIndex + slides.length) % slides.length;
        Array.prototype.forEach.call(slides, function (slide, slideIndex) {
          var active = slideIndex === index;
          slide.classList.toggle("is-active", active);
          slide.setAttribute("aria-hidden", active ? "false" : "true");
        });
        Array.prototype.forEach.call(dots, function (dot, dotIndex) {
          var active = dotIndex === index;
          dot.classList.toggle("is-active", active);
          dot.setAttribute("aria-current", active ? "true" : "false");
        });
        if (counter) {
          counter.textContent = String(index + 1).padStart(2, "0") + " / " + String(slides.length).padStart(2, "0");
        }
      }

      function stop() {
        if (timer) {
          window.clearInterval(timer);
          timer = 0;
        }
      }

      function start() {
        if (reducedMotion || slides.length < 2 || timer) {
          return;
        }
        timer = window.setInterval(function () {
          show(index + 1);
        }, 7000);
      }

      if (prev) {
        prev.addEventListener("click", function () {
          show(index - 1);
          stop();
          start();
        });
      }

      if (next) {
        next.addEventListener("click", function () {
          show(index + 1);
          stop();
          start();
        });
      }

      Array.prototype.forEach.call(dots, function (dot, dotIndex) {
        dot.addEventListener("click", function () {
          show(dotIndex);
          stop();
          start();
        });
      });

      carousel.addEventListener("keydown", function (event) {
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          show(index - 1);
          stop();
          start();
        }
        if (event.key === "ArrowRight") {
          event.preventDefault();
          show(index + 1);
          stop();
          start();
        }
      });
      carousel.addEventListener("touchstart", function (event) {
        carouselTouchStartX = event.changedTouches[0].clientX;
        stop();
      }, { passive: true });
      carousel.addEventListener("touchend", function (event) {
        var delta = event.changedTouches[0].clientX - carouselTouchStartX;
        if (Math.abs(delta) > 44) {
          show(index + (delta > 0 ? -1 : 1));
        }
        start();
      }, { passive: true });
      carousel.addEventListener("mouseenter", stop);
      carousel.addEventListener("mouseleave", start);
      carousel.addEventListener("focusin", stop);
      carousel.addEventListener("focusout", function (event) {
        if (!carousel.contains(event.relatedTarget)) {
          start();
        }
      });
      show(0);
      start();
    });
  }

  function initCarousels() {
    var carousels = document.querySelectorAll("[data-carousel]");
    Array.prototype.forEach.call(carousels, function (carousel) {
      var slides = carousel.querySelectorAll("[data-slide]");
      var prev = carousel.querySelector("[data-carousel-prev]");
      var next = carousel.querySelector("[data-carousel-next]");
      var dots = carousel.querySelectorAll("[data-carousel-dot]");
      var counter = carousel.querySelector("[data-carousel-counter]");
      var pauseButton = carousel.querySelector("[data-carousel-pause]");
      var pulse = carousel.querySelector("[data-music-pulse]");
      var index = 0;
      var timer = 0;
      var paused = false;
      var carouselTouchStartX = 0;
      var pulseTimer = 0;

      if (!slides.length) {
        return;
      }

      function pulseScore(origin) {
        if (!pulse || reducedMotion) {
          return;
        }
        window.clearTimeout(pulseTimer);
        pulse.style.setProperty("--pulse-origin", origin || "50%");
        pulse.classList.remove("is-pulsing");
        window.requestAnimationFrame(function () {
          pulse.classList.add("is-pulsing");
          pulseTimer = window.setTimeout(function () {
            pulse.classList.remove("is-pulsing");
          }, 720);
        });
      }

      function show(newIndex, shouldPulse) {
        index = (newIndex + slides.length) % slides.length;
        carousel.style.setProperty("--carousel-index", String(index));
        Array.prototype.forEach.call(slides, function (slide, slideIndex) {
          var active = slideIndex === index;
          var links = slide.querySelectorAll("a, button");
          slide.classList.toggle("is-active", active);
          slide.setAttribute("aria-hidden", active ? "false" : "true");
          Array.prototype.forEach.call(links, function (link) {
            link.tabIndex = active ? 0 : -1;
          });
        });
        Array.prototype.forEach.call(dots, function (dot, dotIndex) {
          var active = dotIndex === index;
          dot.classList.toggle("is-active", active);
          dot.setAttribute("aria-current", active ? "true" : "false");
        });
        if (counter) {
          counter.textContent = String(index + 1).padStart(2, "0") + " / " + String(slides.length).padStart(2, "0");
        }
        if (shouldPulse !== false) {
          pulseScore(((index + 0.5) / slides.length * 100).toFixed(1) + "%");
        }
      }

      function stop() {
        if (timer) {
          window.clearInterval(timer);
          timer = 0;
        }
      }

      function start() {
        if (reducedMotion || paused || slides.length < 2 || timer || carousel.getAttribute("data-score-state") === "playing") {
          return;
        }
        timer = window.setInterval(function () {
          if (carousel.getAttribute("data-score-state") === "playing") {
            stop();
            return;
          }
          show(index + 1, true);
        }, 7800);
      }

      function updatePauseButton() {
        if (!pauseButton) {
          return;
        }
        var pauseText = pauseButton.getAttribute("data-pause-text") || "Pause";
        var playText = pauseButton.getAttribute("data-play-text") || "Resume";
        pauseButton.textContent = paused ? playText : pauseText;
        pauseButton.setAttribute("aria-label", paused ? playText : pauseText);
        pauseButton.setAttribute("aria-pressed", paused ? "true" : "false");
      }

      if (prev) {
        prev.addEventListener("click", function () {
          show(index - 1, true);
          stop();
          start();
        });
      }

      if (next) {
        next.addEventListener("click", function () {
          show(index + 1, true);
          stop();
          start();
        });
      }

      Array.prototype.forEach.call(dots, function (dot, dotIndex) {
        dot.addEventListener("click", function () {
          show(dotIndex, true);
          stop();
          start();
        });
      });

      if (pauseButton) {
        if (reducedMotion) {
          pauseButton.hidden = true;
        } else {
          pauseButton.addEventListener("click", function () {
            paused = !paused;
            if (paused) {
              stop();
            } else {
              start();
            }
            updatePauseButton();
          });
        }
      }

      carousel.addEventListener("conservatory:scoreplay", stop);
      carousel.addEventListener("conservatory:scoreend", start);

      carousel.addEventListener("keydown", function (event) {
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          show(index - 1, true);
          stop();
          start();
        }
        if (event.key === "ArrowRight") {
          event.preventDefault();
          show(index + 1, true);
          stop();
          start();
        }
      });
      carousel.addEventListener("touchstart", function (event) {
        carouselTouchStartX = event.changedTouches[0].clientX;
        stop();
      }, { passive: true });
      carousel.addEventListener("touchend", function (event) {
        var delta = event.changedTouches[0].clientX - carouselTouchStartX;
        if (Math.abs(delta) > 44) {
          show(index + (delta > 0 ? -1 : 1), true);
        }
        start();
      }, { passive: true });
      carousel.addEventListener("mouseenter", stop);
      carousel.addEventListener("mouseleave", start);
      carousel.addEventListener("focusin", stop);
      carousel.addEventListener("focusout", function (event) {
        if (!carousel.contains(event.relatedTarget)) {
          start();
        }
      });
      show(0, false);
      updatePauseButton();
      start();
    });
  }

  function initImageFallbacks() {
    Array.prototype.forEach.call(document.querySelectorAll("img[data-fallback-src]"), function (image) {
      image.addEventListener("error", function () {
        var fallback = image.getAttribute("data-fallback-src");
        image.removeAttribute("data-fallback-src");
        if (fallback) {
          image.src = fallback;
        }
      });
    });
  }

  function initFilters() {
    var groups = document.querySelectorAll("[data-filter-group]");
    Array.prototype.forEach.call(groups, function (group) {
      var buttons = group.querySelectorAll("[data-filter]");
      var targetSelector = group.getAttribute("data-filter-target");
      var items = targetSelector ? document.querySelectorAll(targetSelector) : [];

      Array.prototype.forEach.call(buttons, function (button) {
        button.addEventListener("click", function () {
          var filter = button.getAttribute("data-filter");
          Array.prototype.forEach.call(buttons, function (item) {
            item.classList.remove("is-active");
            item.setAttribute("aria-pressed", "false");
          });
          button.classList.add("is-active");
          button.setAttribute("aria-pressed", "true");

          Array.prototype.forEach.call(items, function (item) {
            var values = (item.getAttribute("data-filter-values") || "").split(/\s+/);
            var visible = filter === "all" || values.indexOf(filter) !== -1;
            item.hidden = !visible;
          });
        });
      });
    });
  }

  function updateLightbox() {
    if (!lightbox || !lightboxItems.length) {
      return;
    }
    var item = lightboxItems[lightboxIndex];
    var image = lightbox.querySelector("[data-lightbox-image]");
    var caption = lightbox.querySelector("[data-lightbox-caption]");
    if (image) {
      image.src = item.getAttribute("data-lightbox-src");
      image.alt = item.getAttribute("data-lightbox-alt") || "";
    }
    if (caption) {
      caption.textContent = item.getAttribute("data-lightbox-caption") || "";
    }
  }

  function openLightbox(trigger) {
    if (!lightbox) {
      return;
    }
    lightboxItems = Array.prototype.slice.call(document.querySelectorAll("[data-lightbox-src]"));
    lightboxIndex = lightboxItems.indexOf(trigger);
    if (lightboxIndex < 0) {
      lightboxIndex = 0;
    }
    lastLightboxTrigger = trigger;
    updateLightbox();
    lightbox.classList.add("is-open");
    lightbox.setAttribute("aria-hidden", "false");
    body.classList.add("lightbox-open");
    var close = lightbox.querySelector("[data-lightbox-close]");
    if (close) {
      close.focus();
    }
  }

  function closeLightbox() {
    if (!lightbox || !lightbox.classList.contains("is-open")) {
      return;
    }
    lightbox.classList.remove("is-open");
    lightbox.setAttribute("aria-hidden", "true");
    body.classList.remove("lightbox-open");
    if (lastLightboxTrigger) {
      lastLightboxTrigger.focus();
    }
  }

  function stepLightbox(direction) {
    if (!lightboxItems.length) {
      return;
    }
    lightboxIndex = (lightboxIndex + direction + lightboxItems.length) % lightboxItems.length;
    updateLightbox();
  }

  if (lightbox) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-lightbox-src]"), function (trigger) {
      trigger.addEventListener("click", function () {
        openLightbox(trigger);
      });
    });

    var closeButton = lightbox.querySelector("[data-lightbox-close]");
    var prevButton = lightbox.querySelector("[data-lightbox-prev]");
    var nextButton = lightbox.querySelector("[data-lightbox-next]");
    if (closeButton) {
      closeButton.addEventListener("click", closeLightbox);
    }
    if (prevButton) {
      prevButton.addEventListener("click", function () {
        stepLightbox(-1);
      });
    }
    if (nextButton) {
      nextButton.addEventListener("click", function () {
        stepLightbox(1);
      });
    }
    lightbox.addEventListener("click", function (event) {
      if (event.target === lightbox) {
        closeLightbox();
      }
    });
    lightbox.addEventListener("touchstart", function (event) {
      touchStartX = event.changedTouches[0].clientX;
    }, { passive: true });
    lightbox.addEventListener("touchend", function (event) {
      var delta = event.changedTouches[0].clientX - touchStartX;
      if (Math.abs(delta) > 44) {
        stepLightbox(delta > 0 ? -1 : 1);
      }
    }, { passive: true });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      if (lightbox && lightbox.classList.contains("is-open")) {
        closeLightbox();
      } else if (primaryNav && primaryNav.classList.contains("is-open")) {
        closeMenu(true);
      } else {
        closeSubmenus();
      }
    }
    if (lightbox && lightbox.classList.contains("is-open")) {
      if (event.key === "ArrowLeft") {
        stepLightbox(-1);
      }
      if (event.key === "ArrowRight") {
        stepLightbox(1);
      }
    }
  });

  if (hero && !hero.classList.contains("headline-stage") && !reducedMotion && window.matchMedia("(pointer: fine)").matches) {
    hero.addEventListener("pointermove", function (event) {
      var rect = hero.getBoundingClientRect();
      var x = ((event.clientX - rect.left) / rect.width - 0.5) * 20;
      var y = ((event.clientY - rect.top) / rect.height - 0.5) * 14;
      hero.style.setProperty("--hero-x", x.toFixed(2) + "px");
      hero.style.setProperty("--hero-y", y.toFixed(2) + "px");
      hero.style.setProperty("--hero-x-soft", (x * 0.28).toFixed(2) + "px");
      hero.style.setProperty("--hero-y-soft", (y * 0.2).toFixed(2) + "px");
      hero.style.setProperty("--hero-x-opposite", (x * -0.5).toFixed(2) + "px");
      hero.style.setProperty("--hero-y-opposite", (y * -0.4).toFixed(2) + "px");
      hero.style.setProperty("--hero-x-front", (x * -0.72).toFixed(2) + "px");
      hero.style.setProperty("--hero-y-front", (y * -0.58).toFixed(2) + "px");
      hero.style.setProperty("--hero-x-mark", (x * -0.35).toFixed(2) + "px");
      hero.style.setProperty("--hero-y-mark", (y * -0.35).toFixed(2) + "px");
    });
    hero.addEventListener("pointerleave", function () {
      hero.style.setProperty("--hero-x", "0px");
      hero.style.setProperty("--hero-y", "0px");
      hero.style.setProperty("--hero-x-soft", "0px");
      hero.style.setProperty("--hero-y-soft", "0px");
      hero.style.setProperty("--hero-x-opposite", "0px");
      hero.style.setProperty("--hero-y-opposite", "0px");
      hero.style.setProperty("--hero-x-front", "0px");
      hero.style.setProperty("--hero-y-front", "0px");
      hero.style.setProperty("--hero-x-mark", "0px");
      hero.style.setProperty("--hero-y-mark", "0px");
    });
  }

  var topSentinel = document.querySelector("[data-top-sentinel]");
  if (header) {
    if (topSentinel && "IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        header.classList.toggle("is-scrolled", !entries[0].isIntersecting);
      }, { rootMargin: "-24px 0px 0px 0px" }).observe(topSentinel);
    }
  }

  if (backToTop) {
    backToTop.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
    });

    if ("IntersectionObserver" in window) {
      if (topSentinel) {
        new IntersectionObserver(function (entries) {
          backToTop.classList.toggle("is-visible", !entries[0].isIntersecting);
        }).observe(topSentinel);
      }
    } else {
      backToTop.classList.add("is-visible");
    }
  }

  var revealItems = document.querySelectorAll(".reveal, .reveal-mask, .reveal-media, .reveal-date");
  Array.prototype.forEach.call(revealItems, function (item, index) {
    item.style.setProperty("--reveal-delay", Math.min(index % 4, 3) * 45 + "ms");
  });
  if (reducedMotion || !("IntersectionObserver" in window)) {
    Array.prototype.forEach.call(revealItems, function (item) {
      item.classList.add("is-visible");
    });
  } else {
    var revealObserver = new IntersectionObserver(function (entries) {
      Array.prototype.forEach.call(entries, function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          revealObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.06 });
    Array.prototype.forEach.call(revealItems, function (item) {
      revealObserver.observe(item);
    });
  }

  function initLivingScore() {
    var scores = document.querySelectorAll("[data-living-score]");
    var soundToggle = document.querySelector("[data-sound-toggle]");
    var audioContext = null;
    var soundEnabled = false;
    var storedSound = "off";

    try {
      storedSound = window.sessionStorage.getItem("conservatory-sound") || "off";
    } catch (error) {
      storedSound = "off";
    }
    soundEnabled = storedSound === "on";

    function updateSoundToggle() {
      if (!soundToggle) {
        return;
      }
      var label = soundEnabled ? soundToggle.getAttribute("data-label-on") : soundToggle.getAttribute("data-label-off");
      var aria = soundEnabled ? soundToggle.getAttribute("data-aria-on") : soundToggle.getAttribute("data-aria-off");
      soundToggle.textContent = label || "Sound";
      soundToggle.setAttribute("aria-label", aria || label || "Sound");
      soundToggle.setAttribute("aria-pressed", soundEnabled ? "true" : "false");
    }

    function ensureAudio() {
      var Context = window.AudioContext || window.webkitAudioContext;
      if (!Context) {
        return null;
      }
      if (!audioContext) {
        audioContext = new Context();
      }
      if (audioContext.state === "suspended") {
        audioContext.resume();
      }
      return audioContext;
    }

    function playTone(frequency) {
      var context = ensureAudio();
      if (!context || !soundEnabled) {
        return;
      }
      var now = context.currentTime;
      var master = context.createGain();
      var partials = [
        { ratio: 1, level: 0.036 },
        { ratio: 2, level: 0.014 },
        { ratio: 3, level: 0.006 }
      ];
      master.gain.setValueAtTime(0.0001, now);
      master.gain.exponentialRampToValueAtTime(1, now + 0.018);
      master.gain.exponentialRampToValueAtTime(0.42, now + 0.16);
      master.gain.exponentialRampToValueAtTime(0.0001, now + 0.62);
      master.connect(context.destination);
      partials.forEach(function (partial) {
        var oscillator = context.createOscillator();
        var partialGain = context.createGain();
        oscillator.type = "sine";
        oscillator.frequency.setValueAtTime(frequency * partial.ratio, now);
        partialGain.gain.setValueAtTime(partial.level, now);
        oscillator.connect(partialGain);
        partialGain.connect(master);
        oscillator.start(now);
        oscillator.stop(now + 0.64);
      });
    }

    function pulseAt(origin) {
      var pulse = document.querySelector("[data-music-pulse]");
      if (!pulse || reducedMotion) {
        return;
      }
      pulse.style.setProperty("--pulse-origin", origin);
      pulse.classList.remove("is-pulsing");
      window.requestAnimationFrame(function () {
        pulse.classList.add("is-pulsing");
        window.setTimeout(function () {
          pulse.classList.remove("is-pulsing");
        }, 720);
      });
    }

    if (soundToggle) {
      soundToggle.addEventListener("click", function () {
        soundEnabled = !soundEnabled;
        if (soundEnabled) {
          ensureAudio();
        }
        try {
          window.sessionStorage.setItem("conservatory-sound", soundEnabled ? "on" : "off");
        } catch (error) {
          storedSound = soundEnabled ? "on" : "off";
        }
        updateSoundToggle();
      });
      updateSoundToggle();
    }

    Array.prototype.forEach.call(scores, function (score) {
      var notes = score.querySelectorAll("[data-score-note]");
      var scoreRect = null;
      var frame = 0;
      var pointerX = 0;
      var pointerY = 0;

      Array.prototype.forEach.call(notes, function (note) {
        note.addEventListener("click", function () {
          var frequency = Number(note.getAttribute("data-frequency"));
          var origin = note.style.getPropertyValue("--note-x") || "50%";
          if (frequency) {
            playTone(frequency);
          }
          note.classList.add("is-sounding");
          pulseAt(origin);
          window.setTimeout(function () {
            note.classList.remove("is-sounding");
          }, 520);
        });
      });

      if (reducedMotion || !window.matchMedia || !window.matchMedia("(pointer: fine)").matches) {
        return;
      }

      function renderPointer() {
        if (!scoreRect) {
          frame = 0;
          return;
        }
        var xRatio = Math.max(0, Math.min(1, (pointerX - scoreRect.left) / scoreRect.width));
        var yRatio = Math.max(0, Math.min(1, (pointerY - scoreRect.top) / scoreRect.height));
        score.style.setProperty("--score-x", ((xRatio - 0.5) * 7).toFixed(2) + "px");
        score.style.setProperty("--score-y", ((yRatio - 0.5) * 4).toFixed(2) + "px");
        score.style.setProperty("--staff-shift", ((yRatio - 0.5) * 2.5).toFixed(2) + "px");
        Array.prototype.forEach.call(notes, function (note) {
          var noteX = parseFloat(note.style.getPropertyValue("--note-x")) / 100 * scoreRect.width + scoreRect.left;
          var noteY = parseFloat(note.style.getPropertyValue("--note-y")) / 100 * scoreRect.height + scoreRect.top;
          var distance = Math.sqrt(Math.pow(pointerX - noteX, 2) + Math.pow(pointerY - noteY, 2));
          var lift = distance < 170 ? -3.5 * (1 - distance / 170) : 0;
          note.style.setProperty("--note-lift", lift.toFixed(2) + "px");
        });
        frame = 0;
      }

      score.parentNode.addEventListener("pointerenter", function () {
        scoreRect = score.getBoundingClientRect();
      });
      score.parentNode.addEventListener("pointermove", function (event) {
        pointerX = event.clientX;
        pointerY = event.clientY;
        if (!frame) {
          frame = window.requestAnimationFrame(renderPointer);
        }
      });
      score.parentNode.addEventListener("pointerleave", function () {
        scoreRect = null;
        score.style.setProperty("--score-x", "0px");
        score.style.setProperty("--score-y", "0px");
        score.style.setProperty("--staff-shift", "0px");
        Array.prototype.forEach.call(notes, function (note) {
          note.style.setProperty("--note-lift", "0px");
        });
      });
    });
  }

  function initMagneticTargets() {
    if (reducedMotion || !window.matchMedia || !window.matchMedia("(pointer: fine)").matches) {
      return;
    }
    Array.prototype.forEach.call(document.querySelectorAll("[data-magnetic]"), function (target) {
      var rect = null;
      var frame = 0;
      var pointerX = 0;
      var pointerY = 0;
      target.addEventListener("pointerenter", function () {
        rect = target.getBoundingClientRect();
      });
      target.addEventListener("pointermove", function (event) {
        pointerX = event.clientX;
        pointerY = event.clientY;
        if (!frame) {
          frame = window.requestAnimationFrame(function () {
            if (rect) {
              var x = Math.max(-4, Math.min(4, (pointerX - (rect.left + rect.width / 2)) * 0.08));
              var y = Math.max(-3, Math.min(3, (pointerY - (rect.top + rect.height / 2)) * 0.08));
              target.style.transform = "translate3d(" + x.toFixed(2) + "px," + y.toFixed(2) + "px,0)";
            }
            frame = 0;
          });
        }
      });
      target.addEventListener("pointerleave", function () {
        rect = null;
        target.style.transform = "";
      });
    });
  }

  initCarousels();
  initFilters();
  initImageFallbacks();
  initMagneticTargets();
})();