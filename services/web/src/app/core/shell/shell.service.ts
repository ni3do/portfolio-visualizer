import { Injectable } from '@angular/core';

export interface NavigationLink {
  path: string;
  label: string;
  icon?: string;
}

@Injectable({
  providedIn: 'root'
})
export class ShellService {
  readonly links: NavigationLink[] = [
    { path: '/dashboard', label: 'Dashboard', icon: 'insights' },
    { path: '/positions', label: 'Positions', icon: 'table_chart' },
    { path: '/exposures', label: 'Exposures', icon: 'public' },
    { path: '/trades', label: 'Trades', icon: 'swap_horiz' },
    { path: '/instrument-mapping', label: 'Instrument Mapping', icon: 'link' },
    { path: '/settings', label: 'Settings', icon: 'settings' }
  ];
}
