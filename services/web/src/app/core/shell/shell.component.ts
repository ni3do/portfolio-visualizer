import { CommonModule } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { RouterModule } from '@angular/router';
import { Observable } from 'rxjs';

import { NavigationLink, ShellService } from './shell.service';
import { ThemeService, Theme } from '../services/theme.service';

@Component({
  selector: 'app-shell',
  standalone: true,
  imports: [CommonModule, RouterModule],
  templateUrl: './shell.component.html',
  styleUrl: './shell.component.scss'
})
export class ShellComponent {
  private readonly shellService = inject(ShellService);
  private readonly themeService = inject(ThemeService);

  readonly links = computed<NavigationLink[]>(() => this.shellService.links);
  readonly theme$: Observable<Theme> = this.themeService.theme$;
  readonly isSidebarOpen = signal(false);

  toggleTheme(): void {
    this.themeService.toggleTheme();
  }

  toggleSidebar(): void {
    this.isSidebarOpen.update((open) => !open);
  }

  closeSidebar(): void {
    if (this.isSidebarOpen()) {
      this.isSidebarOpen.set(false);
    }
  }

  handleNavigation(): void {
    this.closeSidebar();
  }
}
