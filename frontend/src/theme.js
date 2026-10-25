import { useState, useEffect, useCallback } from 'react';
import { flushSync } from 'react-dom';

export const THEME_STORAGE_KEY = 'oas_theme';
const THEME_COLORS = { dark: '#070b16', light: '#f5f7fc' };

export function getSystemTheme() {
  if (typeof window === 'undefined' || !window.matchMedia) return 'dark';
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

export function applyThemeToDocument(resolved) {
  const root = document.documentElement;
  root.setAttribute('data-theme', resolved);
  root.style.colorScheme = resolved;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEME_COLORS[resolved] || THEME_COLORS.dark);
}

function readPreference() {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch (err) {
    return 'system';
  }
}

/**
 * Theme preference: 'system' | 'light' | 'dark'.
 * Switching plays a circular reveal from the control via the View Transitions API
 * (WAAPI + clip-path, 420ms ease-in-out). Falls back to an instant swap when the API
 * is unavailable or the user prefers reduced motion.
 */
export function useTheme() {
  const [preference, setPreference] = useState(readPreference);
  const [systemTheme, setSystemTheme] = useState(getSystemTheme);

  useEffect(() => {
    if (!window.matchMedia) return undefined;
    const mq = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = (e) => setSystemTheme(e.matches ? 'light' : 'dark');
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else mq.addListener(onChange);
    return () => {
      if (mq.removeEventListener) mq.removeEventListener('change', onChange);
      else mq.removeListener(onChange);
    };
  }, []);

  const resolved = preference === 'system' ? systemTheme : preference;

  useEffect(() => {
    applyThemeToDocument(resolved);
  }, [resolved]);

  const choose = useCallback(
    (next, event) => {
      try {
        localStorage.setItem(THEME_STORAGE_KEY, next);
      } catch (err) {
        /* ignore */
      }
      const nextResolved = next === 'system' ? getSystemTheme() : next;
      const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

      if (nextResolved === resolved || !document.startViewTransition || reduceMotion) {
        setPreference(next);
        return;
      }

      // Origin of the reveal: pointer position, or the centre of the control for keyboard users.
      let x = window.innerWidth - 40;
      let y = 28;
      if (event && typeof event.clientX === 'number' && (event.clientX || event.clientY)) {
        x = event.clientX;
        y = event.clientY;
      } else if (event && event.currentTarget && event.currentTarget.getBoundingClientRect) {
        const r = event.currentTarget.getBoundingClientRect();
        x = r.left + r.width / 2;
        y = r.top + r.height / 2;
      }
      const endRadius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));

      const transition = document.startViewTransition(() => {
        flushSync(() => setPreference(next));
        applyThemeToDocument(nextResolved);
      });

      transition.ready
        .then(() => {
          document.documentElement.animate(
            { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${endRadius}px at ${x}px ${y}px)`] },
            {
              duration: 420,
              easing: 'cubic-bezier(0.77, 0, 0.175, 1)',
              pseudoElement: '::view-transition-new(root)'
            }
          );
        })
        .catch(() => {});
    },
    [resolved]
  );

  const toggle = useCallback(
    (event) => choose(resolved === 'dark' ? 'light' : 'dark', event),
    [choose, resolved]
  );

  return { preference, resolved, choose, toggle };
}
