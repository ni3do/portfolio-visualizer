import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { AsyncPipe, DatePipe } from '@angular/common';
import { Observable } from 'rxjs';

import { PortfolioApiService } from '../../api/portfolio-api.service';
import { TradesResponse } from '../../api/models';

@Component({
  selector: 'app-trades',
  standalone: true,
  imports: [CommonModule, AsyncPipe, DatePipe],
  templateUrl: './trades.component.html',
  styleUrl: './trades.component.scss'
})
export class TradesComponent {
  readonly trades$: Observable<TradesResponse> = this.portfolioApi.getRecentTrades({ limit: 50 });

  constructor(private readonly portfolioApi: PortfolioApiService) {}
}
