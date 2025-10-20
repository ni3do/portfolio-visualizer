import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';

import { AuthService } from '../services/auth.service';

export const basicAuthInterceptor: HttpInterceptorFn = (req, next) => {
  const authService = inject(AuthService);
  const header = authService.getAuthorizationHeader();
  if (header) {
    req = req.clone({
      setHeaders: {
        Authorization: header
      }
    });
  }
  return next(req);
};
