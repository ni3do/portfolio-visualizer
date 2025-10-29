import { Routes } from '@angular/router';

import { authGuard } from './core/guards/auth.guard';
import { ShellComponent } from './core/shell/shell.component';

export const routes: Routes = [
  {
    path: '',
    component: ShellComponent,
    canActivate: [authGuard],
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'dashboard' },
      {
        path: 'dashboard',
        loadComponent: () =>
          import('./features/dashboard/dashboard.component').then(
            (m) => m.DashboardComponent
          )
      },
      {
        path: 'positions',
        loadComponent: () =>
          import('./features/positions/positions.component').then(
            (m) => m.PositionsComponent
          )
      },
      {
        path: 'exposures',
        loadComponent: () =>
          import('./features/exposures/exposures.component').then(
            (m) => m.ExposuresComponent
          )
      },
      {
        path: 'trades',
        loadComponent: () =>
          import('./features/trades/trades.component').then(
            (m) => m.TradesComponent
          )
      },
      {
        path: 'instrument-mapping',
        loadComponent: () =>
          import('./features/instrument-mapping/instrument-mapping.component').then(
            (m) => m.InstrumentMappingComponent
          )
      },
      {
        path: 'settings',
        loadComponent: () =>
          import('./features/settings/settings.component').then(
            (m) => m.SettingsComponent
          )
      }
    ]
  },
  {
    path: 'login',
    loadComponent: () =>
      import('./features/auth/login.component').then((m) => m.LoginComponent)
  },
  { path: '**', redirectTo: 'dashboard' }
];
