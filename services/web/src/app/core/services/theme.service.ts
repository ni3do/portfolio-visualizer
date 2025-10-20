import { Injectable, Renderer2, RendererFactory2 } from '@angular/core';
import { BehaviorSubject, Observable } from 'rxjs';

export type Theme = 'mocha' | 'latte';

export interface ThemeColors {
  navLine: string;
  navFill: string;
  axis: string;
  grid: string;
  text: string;
  piePalette: string[];
  positive: string;
  negative: string;
  percentLine: string;
}

const COLOR_PRESETS: Record<Theme, ThemeColors> = {
  mocha: {
    navLine: '#89b4fa',
    navFill: 'rgba(137, 180, 250, 0.35)',
    axis: '#bac2de',
    grid: 'rgba(69, 71, 90, 0.4)',
    text: '#cdd6f4',
    piePalette: ['#89b4fa', '#b4befe', '#cba6f7', '#f2cdcd', '#fab387', '#f9e2af', '#a6e3a1', '#94e2d5'],
    positive: '#a6e3a1',
    negative: '#f38ba8',
    percentLine: '#f9e2af'
  },
  latte: {
    navLine: '#1e66f5',
    navFill: 'rgba(30, 102, 245, 0.22)',
    axis: '#5c5f77',
    grid: 'rgba(172, 176, 190, 0.5)',
    text: '#4c4f69',
    piePalette: ['#1e66f5', '#7287fd', '#df8e1d', '#fe640b', '#ea76cb', '#d20f39', '#40a02b', '#179299'],
    positive: '#40a02b',
    negative: '#d20f39',
    percentLine: '#df8e1d'
  }
};

@Injectable({ providedIn: 'root' })
export class ThemeService {
  private readonly storageKey = 'pv-theme';
  private readonly renderer: Renderer2;
  private readonly themeSubject: BehaviorSubject<Theme>;
  readonly theme$: Observable<Theme>;

  constructor(rendererFactory: RendererFactory2) {
    this.renderer = rendererFactory.createRenderer(null, null);
    const initial = this.resolveInitialTheme();
    this.themeSubject = new BehaviorSubject<Theme>(initial);
    this.theme$ = this.themeSubject.asObservable();
    this.applyTheme(initial);
  }

  get currentTheme(): Theme {
    return this.themeSubject.value;
  }

  setTheme(theme: Theme): void {
    if (theme === this.themeSubject.value) {
      return;
    }
    this.themeSubject.next(theme);
    this.applyTheme(theme);
    this.persistTheme(theme);
  }

  toggleTheme(): void {
    this.setTheme(this.themeSubject.value === 'mocha' ? 'latte' : 'mocha');
  }

  getColors(theme: Theme = this.themeSubject.value): ThemeColors {
    return COLOR_PRESETS[theme];
  }

  private resolveInitialTheme(): Theme {
    if (typeof window !== 'undefined') {
      try {
        const stored = window.localStorage.getItem(this.storageKey) as Theme | null;
        if (stored === 'mocha' || stored === 'latte') {
          return stored;
        }
      } catch (err) {
        // ignore storage errors
      }
      if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        return 'mocha';
      }
    }
    return 'mocha';
  }

  private applyTheme(theme: Theme): void {
    if (typeof document === 'undefined') {
      return;
    }
    const body = document.body;
    this.renderer.removeClass(body, 'theme-mocha');
    this.renderer.removeClass(body, 'theme-latte');
    this.renderer.addClass(body, `theme-${theme}`);
    document.documentElement.style.setProperty('color-scheme', theme === 'mocha' ? 'dark' : 'light');
  }

  private persistTheme(theme: Theme): void {
    if (typeof window === 'undefined') {
      return;
    }
    try {
      window.localStorage.setItem(this.storageKey, theme);
    } catch (err) {
      // ignore storage errors
    }
  }
}
