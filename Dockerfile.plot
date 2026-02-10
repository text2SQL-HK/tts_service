FROM python:3.10-slim

WORKDIR /home

COPY requirements_plot.txt .

RUN pip install --no-cache-dir -r requirements_plot.txt

CMD ["sleep", "infinity"]
