document.addEventListener('DOMContentLoaded', () => {

  // --- Theme toggle (system → light → dark → system) ---
  const themeBtn = document.getElementById('theme-btn');
  const icons = {
    system: document.getElementById('theme-icon-system'),
    light: document.getElementById('theme-icon-light'),
    dark: document.getElementById('theme-icon-dark'),
  };

  const themes = ['system', 'light', 'dark'];

  function getStoredTheme() {
    return localStorage.getItem('theme') || 'system';
  }

  function applyTheme(theme) {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const isDark = theme === 'dark' || (theme === 'system' && prefersDark);
    document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
    // Swap screenshot
    const img = document.getElementById('screenshot');
    if (img) {
      img.src = isDark ? 'assets/Screenshot_Dark_Mode.png' : 'assets/Screenshot_Light_Mode.png';
    }
  }

  function updateThemeIcon(theme) {
    Object.keys(icons).forEach(k => icons[k].classList.add('hidden'));
    icons[theme].classList.remove('hidden');
  }

  function cycleTheme() {
    const current = getStoredTheme();
    const idx = themes.indexOf(current);
    const next = themes[(idx + 1) % themes.length];
    localStorage.setItem('theme', next);
    applyTheme(next);
    updateThemeIcon(next);
  }

  if (themeBtn) {
    const stored = getStoredTheme();
    applyTheme(stored);
    updateThemeIcon(stored);
    themeBtn.addEventListener('click', cycleTheme);
  }

  // Listen for system theme changes (only relevant in 'system' mode)
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (getStoredTheme() === 'system') applyTheme('system');
  });

  // --- Copy to clipboard ---
  document.querySelectorAll('[data-copy]').forEach(btn => {
    btn.addEventListener('click', () => {
      const text = btn.dataset.copy.replace(/&amp;/g, '&');
      const original = btn.textContent;
      navigator.clipboard.writeText(text).then(() => {
        btn.textContent = 'Copied!';
        btn.classList.add('text-aw-green', 'border-aw-green');
        setTimeout(() => {
          btn.textContent = original;
          btn.classList.remove('text-aw-green', 'border-aw-green');
        }, 1500);
      });
    });
  });

  // --- Installation tab switcher ---
  const tabBtns = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');

  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      tabBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      const target = btn.dataset.tab;
      tabContents.forEach(tc => tc.classList.add('hidden'));
      const el = document.getElementById('tab-' + target);
      if (el) el.classList.remove('hidden');
    });
  });

  // --- Mobile hamburger menu ---
  const menuBtn = document.getElementById('menu-btn');
  const mobileMenu = document.getElementById('mobile-menu');

  if (menuBtn && mobileMenu) {
    menuBtn.addEventListener('click', () => {
      mobileMenu.classList.toggle('hidden');
    });

    mobileMenu.querySelectorAll('a').forEach(link => {
      link.addEventListener('click', () => {
        mobileMenu.classList.add('hidden');
      });
    });
  }

  // --- Smooth scroll + active nav highlight ---
  const sections = document.querySelectorAll('section[id]');
  const navLinks = document.querySelectorAll('.nav-link');

  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const id = entry.target.id;
        navLinks.forEach(link => {
          const href = link.getAttribute('href');
          if (href === '#' + id) {
            link.classList.add('text-aw-accent');
          } else if (href && href.startsWith('#')) {
            link.classList.remove('text-aw-accent');
          }
        });
      }
    });
  }, { rootMargin: '-80px 0px -50% 0px' });

  sections.forEach(s => observer.observe(s));

  navLinks.forEach(link => {
    link.addEventListener('click', e => {
      const href = link.getAttribute('href');
      if (!href || !href.startsWith('#')) return;
      e.preventDefault();
      const target = document.querySelector(href);
      if (target) {
        target.scrollIntoView({ behavior: 'smooth' });
      }
    });
  });

});
