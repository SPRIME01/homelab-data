#!/usr/bin/env python3

import json
import yaml
import logging
import pandas as pd
import numpy as np
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, Union
from datetime import datetime, timedelta
from dataclasses import dataclass
from functools import reduce

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("transformers")

class BaseTransformer(ABC):
    """Base class for all transformers."""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}

    @abstractmethod
    def transform(self, data: Any) -> Any:
        """Transform input data."""
        pass

    def __call__(self, data: Any) -> Any:
        """Make transformer callable."""
        return self.transform(data)

    def __rshift__(self, other: 'BaseTransformer') -> 'CompositeTransformer':
        """Enable transformer composition using >>."""
        return CompositeTransformer([self, other])

class CompositeTransformer(BaseTransformer):
    """Combines multiple transformers into a pipeline."""

    def __init__(self, transformers: List[BaseTransformer]):
        super().__init__()
        self.transformers = transformers

    def transform(self, data: Any) -> Any:
        """Apply transformers in sequence."""
        return reduce(lambda d, t: t(d), self.transformers, data)

class UnitConverter(BaseTransformer):
    """Converts values between different units."""

    CONVERSIONS = {
        "temperature": {
            "C_to_F": lambda x: x * 9/5 + 32,
            "F_to_C": lambda x: (x - 32) * 5/9,
            "C_to_K": lambda x: x + 273.15,
            "K_to_C": lambda x: x - 273.15
        },
        "energy": {
            "kwh_to_joules": lambda x: x * 3600000,
            "joules_to_kwh": lambda x: x / 3600000,
            "watts_to_kilowatts": lambda x: x / 1000
        },
        "pressure": {
            "pa_to_hpa": lambda x: x / 100,
            "hpa_to_mmhg": lambda x: x * 0.75006
        }
    }

    def __init__(self, config: Dict):
        super().__init__(config)
        self.from_unit = config["from_unit"]
        self.to_unit = config["to_unit"]
        self.conversion_type = config["type"]

    def transform(self, data: Union[float, Dict]) -> Union[float, Dict]:
        """Convert value from one unit to another."""
        if isinstance(data, dict):
            value = data.get("value")
            if value is None:
                raise ValueError("Dictionary input must contain 'value' key")
        else:
            value = data

        conversion_key = f"{self.from_unit}_to_{self.to_unit}"
        converter = self.CONVERSIONS[self.conversion_type].get(conversion_key)

        if not converter:
            raise ValueError(f"Unsupported conversion: {conversion_key}")

        converted = converter(value)

        if isinstance(data, dict):
            return {**data, "value": converted, "unit": self.to_unit}
        return converted

class TimeSeriesAggregator(BaseTransformer):
    """Aggregates time series data."""

    AGGREGATIONS = {
        "mean": np.mean,
        "max": np.max,
        "min": np.min,
        "sum": np.sum,
        "std": np.std,
        "count": len,
        "median": np.median,
        "percentile": lambda x, q: np.percentile(x, q)
    }

    def __init__(self, config: Dict):
        super().__init__(config)
        self.window = pd.Timedelta(config["window"])
        self.aggregation = config["aggregation"]
        self.timestamp_field = config.get("timestamp_field", "timestamp")
        self.value_field = config.get("value_field", "value")

    def transform(self, data: List[Dict]) -> List[Dict]:
        """Aggregate time series data."""
        # Convert to DataFrame
        df = pd.DataFrame(data)
        df[self.timestamp_field] = pd.to_datetime(df[self.timestamp_field])
        df.set_index(self.timestamp_field, inplace=True)

        # Apply aggregation
        if self.aggregation in self.AGGREGATIONS:
            agg_func = self.AGGREGATIONS[self.aggregation]
            result = df.resample(self.window)[self.value_field].agg(agg_func)
        elif self.aggregation == "percentile":
            q = self.config.get("percentile", 95)
            result = df.resample(self.window)[self.value_field].agg(
                lambda x: self.AGGREGATIONS["percentile"](x, q)
            )
        else:
            raise ValueError(f"Unsupported aggregation: {self.aggregation}")

        # Convert back to list of dicts
        return [
            {
                self.timestamp_field: timestamp.isoformat(),
                self.value_field: value,
                "aggregation": self.aggregation,
                "window": str(self.window)
            }
            for timestamp, value in result.items()
        ]

class EventCorrelator(BaseTransformer):
    """Correlates events based on time and conditions."""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.window = timedelta(seconds=config.get("window_seconds", 300))
        self.conditions = config.get("conditions", [])
        self.group_by = config.get("group_by", [])
        self.correlation_field = config.get("correlation_field", "event_type")

    def transform(self, events: List[Dict]) -> List[Dict]:
        """Find correlated events within time window."""
        # Convert to DataFrame
        df = pd.DataFrame(events)
        df["timestamp"] = pd.to_datetime(df["timestamp"])

        correlated_events = []
        for _, group in df.groupby(self.group_by or [True]):
            # Sort by timestamp
            group = group.sort_values("timestamp")

            # Find events within window
            for i, event in group.iterrows():
                window_end = event["timestamp"] + self.window
                related = group[
                    (group["timestamp"] >= event["timestamp"]) &
                    (group["timestamp"] <= window_end)
                ]

                # Check conditions
                matches = True
                for condition in self.conditions:
                    field = condition["field"]
                    op = condition["operator"]
                    value = condition["value"]

                    if op == "equals":
                        matches &= any(related[field] == value)
                    elif op == "differs":
                        matches &= any(related[field] != value)
                    elif op == "in":
                        matches &= any(related[field].isin(value))

                if matches:
                    correlated_events.append({
                        "timestamp": event["timestamp"].isoformat(),
                        "trigger_event": event.to_dict(),
                        "correlated_events": related.to_dict("records"),
                        "correlation_type": self.correlation_field,
                        "window_seconds": self.window.total_seconds()
                    })

        return correlated_events

class DataEnricher(BaseTransformer):
    """Enriches data with information from external sources."""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.enrichments = config["enrichments"]
        self.cache = {}
        self.cache_ttl = timedelta(seconds=config.get("cache_ttl", 300))
        self.last_refresh = {}

    def transform(self, data: Dict) -> Dict:
        """Enrich data with external information."""
        result = data.copy()

        for enrichment in self.enrichments:
            source = enrichment["source"]
            target_field = enrichment["target_field"]

            # Check cache
            if source in self.cache:
                last_refresh = self.last_refresh.get(source)
                if last_refresh and datetime.now() - last_refresh <= self.cache_ttl:
                    result[target_field] = self.cache[source]
                    continue

            # Load enrichment data
            if enrichment["type"] == "file":
                with open(enrichment["path"]) as f:
                    if enrichment["path"].endswith(".json"):
                        enrichment_data = json.load(f)
                    elif enrichment["path"].endswith(".yaml"):
                        enrichment_data = yaml.safe_load(f)
                    else:
                        enrichment_data = f.read()

            elif enrichment["type"] == "http":
                import requests
                response = requests.get(
                    enrichment["url"],
                    headers=enrichment.get("headers", {}),
                    timeout=enrichment.get("timeout", 5)
                )
                response.raise_for_status()
                enrichment_data = response.json()

            elif enrichment["type"] == "database":
                # Example database enrichment
                pass

            # Apply mapping
            if "mapping" in enrichment:
                mapping = enrichment["mapping"]
                if isinstance(mapping, dict):
                    enrichment_data = {
                        k: enrichment_data.get(v)
                        for k, v in mapping.items()
                    }
                elif callable(mapping):
                    enrichment_data = mapping(enrichment_data)

            # Update cache
            self.cache[source] = enrichment_data
            self.last_refresh[source] = datetime.now()

            # Add to result
            result[target_field] = enrichment_data

        return result

class FormatConverter(BaseTransformer):
    """Converts data between different formats."""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.input_format = config["input_format"]
        self.output_format = config["output_format"]
        self.schema = config.get("schema")

    def transform(self, data: Any) -> Any:
        """Convert data between formats."""
        # Parse input
        if isinstance(data, str):
            if self.input_format == "json":
                parsed = json.loads(data)
            elif self.input_format == "yaml":
                parsed = yaml.safe_load(data)
            else:
                parsed = data
        else:
            parsed = data

        # Validate against schema if provided
        if self.schema:
            import jsonschema
            jsonschema.validate(parsed, self.schema)

        # Convert to output format
        if self.output_format == "json":
            if self.config.get("pretty", False):
                return json.dumps(parsed, indent=2)
            return json.dumps(parsed)
        elif self.output_format == "yaml":
            return yaml.dump(parsed)
        elif self.output_format == "csv":
            import csv
            import io
            output = io.StringIO()
            if isinstance(parsed, list):
                if parsed:
                    fieldnames = list(parsed[0].keys())
                    writer = csv.DictWriter(output, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(parsed)
            return output.getvalue()
        elif self.output_format == "xml":
            import dicttoxml
            return dicttoxml.dicttoxml(parsed)
        else:
            return parsed

# Example usage
if __name__ == "__main__":
    # Unit conversion example
    temp_converter = UnitConverter({
        "type": "temperature",
        "from_unit": "C",
        "to_unit": "F"
    })

    # Time series aggregation example
    aggregator = TimeSeriesAggregator({
        "window": "5min",
        "aggregation": "mean"
    })

    # Format conversion example
    format_converter = FormatConverter({
        "input_format": "json",
        "output_format": "yaml"
    })

    # Chain transformers
    pipeline = temp_converter >> aggregator >> format_converter

    # Example data
    data = [
        {"timestamp": "2023-09-15T10:00:00Z", "value": 20.5},
        {"timestamp": "2023-09-15T10:01:00Z", "value": 21.0},
        {"timestamp": "2023-09-15T10:02:00Z", "value": 21.5}
    ]

    # Process data through pipeline
    result = pipeline(data)
    print(result)
