import { bootstrapApplication } from '@angular/platform-browser';
import { Chart, registerables } from 'chart.js';
import 'chartjs-adapter-date-fns';

import { AppComponent } from './app/app.component';
import { appConfig } from './app/app.config';

Chart.register(...registerables);

bootstrapApplication(AppComponent, appConfig).catch((err) =>
  console.error(err)
);
