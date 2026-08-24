-- Independently fitted signed run-margin target used alongside home/away run regressors.
alter table public.model_artifacts add column if not exists margin_intercept double precision;
alter table public.model_artifacts add column if not exists margin_coefficients jsonb;
