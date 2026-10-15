FROM python:3.13-slim

RUN pip install --no-cache-dir pandas==3.0.3 numpy==2.4.6 requests==2.32.3

WORKDIR /app
ADD https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js ./plotly.min.js
COPY heizkurve.py pv_regler.py bericht_vorlage.html run.py ./

ENV HEIZKURVE_DATEN=/data
CMD ["python", "-u", "run.py"]
