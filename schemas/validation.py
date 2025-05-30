#!/usr/bin/env python3

import os
import json
import glob
import logging
import jsonschema
import copy # Added import
from typing import Dict, Any, Optional, List, Tuple, Union
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from contextlib import contextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("schema_validation")

class ValidationError(Exception):
    """Custom exception for schema validation errors."""
    def __init__(self, message: str, errors: List[Dict] = None, schema_id: str = None):
        self.message = message
        self.errors = errors or []
        self.schema_id = schema_id
        self.timestamp = datetime.utcnow().isoformat()
        super().__init__(self.message)

    def to_dict(self) -> Dict:
        """Convert the error to a dictionary representation."""
        return {
            "message": self.message,
            "errors": self.errors,
            "schema_id": self.schema_id,
            "timestamp": self.timestamp
        }

class SchemaRegistry:
    """Registry for managing JSON schemas."""
    def __init__(self, schemas_dir: str = None):
        """Initialize the schema registry.

        Args:
            schemas_dir: Directory containing schema files. If None, uses the directory
                        where this file is located, assuming schemas are in the same directory.
        """
        if schemas_dir is None:
            # Default to directory containing this file
            self.schemas_dir = os.path.dirname(os.path.abspath(__file__))
        else:
            self.schemas_dir = schemas_dir

        self._schemas = {}
        self._schema_versions = {}
        self._schema_registry_file = os.path.join(self.schemas_dir, "schema-registry.json")
        self._load_schemas()

    def _load_schemas(self) -> None:
        """Load all schema files from the schemas directory."""
        try:
            # Look for schema registry file first
            if os.path.exists(self._schema_registry_file):
                with open(self._schema_registry_file, 'r') as f:
                    registry_data = json.load(f)

                if "schemas" in registry_data:
                    for schema_entry in registry_data.get("schemas", []):
                        schema_id = schema_entry.get("id")
                        schema_path = schema_entry.get("path")
                        schema_version = schema_entry.get("version")

                        # If path is relative, make it absolute
                        if schema_path and not os.path.isabs(schema_path):
                            schema_path = os.path.join(self.schemas_dir, os.path.normpath(schema_path))

                        if schema_id and schema_path and os.path.exists(schema_path):
                            try:
                                self._load_schema_file(schema_path, schema_id, schema_version)
                            except Exception as e:
                                logger.error(f"Error loading schema {schema_id} from {schema_path}: {e}")

            # Fall back to loading all JSON files in directory if no schemas were loaded
            if not self._schemas:
                logger.info("No schemas loaded from registry, loading from directory")
                schema_files = glob.glob(os.path.join(self.schemas_dir, "*.json"))
                for schema_file in schema_files:
                    if os.path.basename(schema_file) != "schema-registry.json":
                        try:
                            self._load_schema_file(schema_file)
                        except Exception as e:
                            logger.error(f"Error loading schema from {schema_file}: {e}")

            logger.info(f"Loaded {len(self._schemas)} schemas")
        except Exception as e:
            logger.error(f"Error loading schemas: {e}")

    def _load_schema_file(self, file_path: str, schema_id: str = None, version: str = None) -> None:
        """Load a single schema file.

        Args:
            file_path: Path to the schema file
            schema_id: Optional ID to use for the schema
            version: Optional version string for the schema
        """
        try:
            with open(file_path, 'r') as f:
                schema = json.load(f)

            # Use provided ID or extract from schema or filename
            if not schema_id:
                schema_id = (
                    schema.get("$id", "").split("/")[-1].replace(".json", "") or
                    os.path.basename(file_path).replace(".json", "")
                )

            # Use provided version or extract from schema
            if not version:
                version = schema.get("version", "1.0.0")

            # Store schema by ID
            self._schemas[schema_id] = schema

            # Track versions
            if schema_id not in self._schema_versions:
                self._schema_versions[schema_id] = {}

            self._schema_versions[schema_id][version] = schema

            logger.debug(f"Loaded schema {schema_id} version {version} from {file_path}")
        except Exception as e:
            logger.error(f"Error loading schema file {file_path}: {e}")
            raise

    @lru_cache(maxsize=128)
    def get_schema(self, schema_id: str, version: str = None) -> Dict:
        """Get a schema by ID and optional version.

        Args:
            schema_id: ID of the schema to retrieve
            version: Optional version of the schema. If None, returns latest version.

        Returns:
            The schema as a dictionary

        Raises:
            KeyError: If schema with given ID is not found, or version is not found
        """
        if schema_id not in self._schemas:
            raise KeyError(f"Schema '{schema_id}' not found")

        if not version:
            # Return the most recently loaded version
            return self._schemas[schema_id]

        if schema_id not in self._schema_versions or version not in self._schema_versions[schema_id]:
            raise KeyError(f"Schema '{schema_id}' version '{version}' not found")

        return self._schema_versions[schema_id][version]

    def get_schema_versions(self, schema_id: str) -> List[str]:
        """Get available versions of a schema.

        Args:
            schema_id: ID of the schema

        Returns:
            List of version strings

        Raises:
            KeyError: If schema with given ID is not found
        """
        if schema_id not in self._schema_versions:
            raise KeyError(f"Schema '{schema_id}' not found")

        return list(self._schema_versions[schema_id].keys())

    def list_schemas(self) -> Dict[str, List[str]]:
        """List all available schemas and their versions.

        Returns:
            Dictionary mapping schema IDs to lists of available versions
        """
        return {schema_id: list(versions.keys())
                for schema_id, versions in self._schema_versions.items()}


class MessageValidator:
    """Validator for messages against JSON schemas."""
    def __init__(self, schema_registry: SchemaRegistry):
        """Initialize the message validator.

        Args:
            schema_registry: SchemaRegistry instance to use for schema lookup
        """
        self.registry = schema_registry

    def validate(self, message: Dict, schema_id: str,
                 version: str = None, strict: bool = True) -> Tuple[bool, Optional[List]]:
        """Validate a message against a schema.

        Args:
            message: Message to validate
            schema_id: ID of the schema to validate against
            version: Optional version of the schema. If None, uses latest version.
            strict: If True, raises ValidationError on failure. If False, returns (False, errors)

        Returns:
            Tuple containing (is_valid, errors)

        Raises:
            ValidationError: If validation fails and strict=True
            KeyError: If schema with given ID is not found
        """
        try:
            schema = self.registry.get_schema(schema_id, version)
            jsonschema.validate(instance=message, schema=schema)
            return True, None
        except jsonschema.exceptions.ValidationError as e:
            # Extract validation errors
            errors = self._format_validation_error(e)

            if strict:
                raise ValidationError(
                    message=f"Message validation failed for schema '{schema_id}'",
                    errors=errors,
                    schema_id=schema_id
                )

            return False, errors
        except KeyError:
            # Re-raise schema not found error
            raise
        except Exception as e:
            error_detail = [{"error": str(e)}]

            if strict:
                raise ValidationError(
                    message=f"Unexpected error validating message against schema '{schema_id}'",
                    errors=error_detail,
                    schema_id=schema_id
                )

            return False, error_detail

    def _format_validation_error(self, error: jsonschema.exceptions.ValidationError) -> List[Dict]:
        """Format a validation error into a structured list of errors.

        Args:
            error: The ValidationError from jsonschema

        Returns:
            List of error dictionaries with path and message
        """
        errors = []

        if error.path:
            path_str = '.'.join(str(p) for p in error.path)
        else:
            path_str = '(root)'

        errors.append({
            "path": path_str,
            "message": error.message,
            "validator": error.validator,
            "validator_value": error.validator_value
        })

        # Include context errors (if any)
        for context_error in error.context:
            errors.extend(self._format_validation_error(context_error))

        return errors

    def validate_with_inferred_schema(self, message: Dict) -> Tuple[bool, str, Optional[List]]:
        """Attempt to validate a message by inferring the schema from the message.

        Args:
            message: Message to validate

        Returns:
            Tuple containing (is_valid, schema_id, errors)
        """
        if not isinstance(message, dict):
            return False, None, [{"error": "Message must be a dictionary"}]

        # Try to find schema_version field first
        schema_id = None
        if "schema_id" in message:
            schema_id = message["schema_id"]
        elif "$schema" in message and "/" in message["$schema"]:
            # Extract schema name from URI/URL reference
            schema_id = message["$schema"].split("/")[-1].replace(".json", "")

        # Try to guess based on message structure
        if not schema_id:
            schema_id = self._guess_schema_type(message)

        if not schema_id:
            return False, None, [{"error": "Could not determine schema for message"}]

        try:
            valid, errors = self.validate(message, schema_id, strict=False)
            return valid, schema_id, errors
        except KeyError:
            return False, schema_id, [{"error": f"Schema '{schema_id}' not found"}]

    def _guess_schema_type(self, message: Dict) -> Optional[str]:
        """Attempt to guess the appropriate schema based on message structure.

        Args:
            message: The message to analyze

        Returns:
            Schema ID string if a match is found, otherwise None
        """
        # Check for sensor reading
        if "sensor_id" in message and "value" in message:
            return "sensor-reading"

        # Check for device state change
        if "entity_id" in message and ("old_state" in message or "new_state" in message):
            return "device-state-change"

        # Check for AI inference request
        if "model_name" in message and "inputs" in message:
            return "ai-inference-request"

        # Check for AI inference response
        if "model_name" in message and "outputs" in message and "request_id" in message:
            return "ai-inference-response"

        # Check for system metric
        if "metric_name" in message and "value" in message and "timestamp" in message:
            return "system-metric"

        # Check for alert notification
        if "alert_id" in message and "severity" in message:
            return "alert-notification"

        # No match found
        return None


class SchemaEnforcer:
    """Utility for enforcing schemas in message flows."""
    def __init__(self, schema_registry: Optional[SchemaRegistry] = None,
                schemas_dir: Optional[str] = None):
        """Initialize the schema enforcer.

        Args:
            schema_registry: Optional schema registry to use.
                           If None, creates a new one.
            schemas_dir: Directory containing schema files.
                       Only used if schema_registry is None.
        """
        if schema_registry:
            self.registry = schema_registry
        else:
            self.registry = SchemaRegistry(schemas_dir)

        self.validator = MessageValidator(self.registry)

    def validate_message(self, message: Dict, schema_id: str,
                       version: str = None) -> Dict:
        """Validate a message and return it if valid, raising an error otherwise.

        Args:
            message: The message to validate
            schema_id: ID of the schema to validate against
            version: Optional version of the schema. If None, uses latest.

        Returns:
            The original message if valid

        Raises:
            ValidationError: If message fails validation
        """
        self.validator.validate(message, schema_id, version, strict=True)

        # Add schema metadata if not present
        if "schema_id" not in message:
            message = {**message, "schema_id": schema_id}

        if "schema_version" not in message:
            message = {**message, "schema_version": version or "1.0.0"}

        return message

    def validate_or_fix_message(self, message: Dict, schema_id: str,
                              version: str = None) -> Dict:
        """Validate a message and attempt to fix common issues.

        Args:
            message: The message to validate
            schema_id: ID of the schema to validate against
            version: Optional version of the schema. If None, uses latest.

        Returns:
            The validated (and possibly fixed) message

        Raises:
            ValidationError: If message cannot be fixed
        """
        # Try validating first
        try:
            return self.validate_message(message, schema_id, version)
        except ValidationError as e:
            # Attempt to fix common issues
            fixed_message = self._attempt_fix_message(message, schema_id, version, e)

            # Validate the fixed message
            return self.validate_message(fixed_message, schema_id, version)

    def _attempt_fix_message(self, message: Dict, schema_id: str,
                           version: str, error: ValidationError) -> Dict:
        """
                           Attempts to automatically fix common validation issues in a message based on schema requirements.
                           
                           Tries to resolve missing required fields by adding default values and inserts a current timestamp if needed. Returns a new, fixed message dictionary.
                           """
        fixed_message = copy.deepcopy(message) # Changed to deepcopy
        schema = self.registry.get_schema(schema_id, version)

        for err in error.errors:
            path = err.get("path")
            field_name = path.split('.')[-1] if path and '.' in path else path

            if field_name == '(root)':
                # Handle missing required fields
                if "required" in schema:
                    for req_field in schema.get("required", []):
                        if req_field not in fixed_message and "properties" in schema:
                            prop_schema = schema["properties"].get(req_field, {})
                            fixed_message[req_field] = self._get_default_value(prop_schema)

            # Add missing timestamp field if it's a common schema
            if "timestamp" in schema.get("properties", {}) and "timestamp" not in fixed_message:
                fixed_message["timestamp"] = datetime.utcnow().isoformat()

        return fixed_message

    def _get_default_value(self, property_schema: Dict) -> Any:
        """Get a default value based on property schema.

        Args:
            property_schema: Schema for a property

        Returns:
            A suitable default value
        """
        if "default" in property_schema:
            return property_schema["default"]

        prop_type = property_schema.get("type")

        if prop_type == "string":
            return ""
        elif prop_type == "number" or prop_type == "integer":
            return 0
        elif prop_type == "boolean":
            return False
        elif prop_type == "array":
            return []
        elif prop_type == "object":
            return {}
        else:
            return None

    @contextmanager
    def enforced_schema(self, schema_id: str, version: str = None):
        """Context manager for enforcing a schema on messages.

        Args:
            schema_id: Schema ID to enforce
            version: Schema version

        Usage:
            with enforcer.enforced_schema("sensor-reading") as validate:
                valid_message = validate(message)
        """
        def validate_func(message: Dict) -> Dict:
            return self.validate_message(message, schema_id, version)

        yield validate_func


# Default global instances for convenience
default_registry = SchemaRegistry()
default_validator = MessageValidator(default_registry)
default_enforcer = SchemaEnforcer(default_registry)


def validate_message(message: Dict, schema_id: str, version: str = None, strict: bool = True) -> bool:
    """Validate a message against a schema using the default validator.

    Args:
        message: The message to validate
        schema_id: ID of the schema to validate against
        version: Optional version string
        strict: If True, raises ValidationError on failure. If False, returns False

    Returns:
        True if valid, False if invalid (only when strict=False)

    Raises:
        ValidationError: If validation fails and strict=True
    """
    try:
        valid, _ = default_validator.validate(message, schema_id, version, strict)
        return valid
    except ValidationError:
        if strict:
            raise
        return False

def get_schema(schema_id: str, version: str = None) -> Dict:
    """Get a schema from the default registry.

    Args:
        schema_id: ID of the schema
        version: Optional version string

    Returns:
        The schema as a dictionary

    Raises:
        KeyError: If schema is not found
    """
    return default_registry.get_schema(schema_id, version)

def list_schemas() -> Dict[str, List[str]]:
    """List all available schemas in the default registry.

    Returns:
        Dictionary mapping schema IDs to lists of versions
    """
    return default_registry.list_schemas()


if __name__ == "__main__":
    # Example usage
    print("Available schemas:", list_schemas())

    # Example validation
    test_message = {
        "sensor_id": "temperature_living_room",
        "value": 21.5,
        "unit": "°C",
        "timestamp": "2023-09-15T14:22:10.500Z"
    }

    try:
        is_valid = validate_message(test_message, "sensor-reading")
        print(f"Validation result: {is_valid}")
    except ValidationError as e:
        print(f"Validation error: {e.message}")
        for error in e.errors:
            print(f"  - {error['path']}: {error['message']}")
