# Message Sequence Flows in Homelab Data Mesh

This document illustrates the key message flows through the homelab data mesh architecture using sequence diagrams.

## 1. Sensor Data Collection and Analysis Flow

```mermaid
sequenceDiagram
    participant Sensor as Sensor/IoT Device
    participant HA as Home Assistant
    participant RMQ as RabbitMQ
    participant Transform as Transformer Service
    participant Influx as InfluxDB
    participant AI as AI Analysis Service
    participant Alert as Alert Manager

    Sensor->>HA: Send sensor reading
    activate HA
    HA->>RMQ: Publish to sensors.{type}.{location}
    deactivate HA

    par Process metrics
        RMQ->>Transform: Route to metrics.process
        activate Transform
        Transform-->>RMQ: Publish transformed data
        deactivate Transform
        RMQ->>Influx: Store metrics
        activate Influx
        Influx-->>RMQ: Acknowledge storage
        deactivate Influx
    and Analyze data
        RMQ->>AI: Route to ai.analysis
        activate AI
        AI-->>RMQ: Publish analysis results
        deactivate AI
    end

    alt Anomaly detected
        RMQ->>Alert: Route to alerts.generated
        activate Alert
        Alert-->>HA: Send notification
        deactivate Alert
    end

    Note over HA,Alert: Complete flow takes 1-2 seconds
```

## 2. AI Inference Request/Response Flow

```mermaid
sequenceDiagram
    participant Client as Client Service
    participant RMQ as RabbitMQ
    participant Handler as Request Handler
    participant Triton as Triton Server
    participant Cache as Redis Cache
    participant Archive as MinIO Archive

    Client->>RMQ: Publish inference request
    activate RMQ
    RMQ->>Handler: Route to ai.inference.requests
    deactivate RMQ

    activate Handler
    Handler->>Cache: Check cache

    alt Cache hit
        Cache-->>Handler: Return cached result
    else Cache miss
        Handler->>Triton: Forward request
        activate Triton
        Triton-->>Handler: Return inference result
        deactivate Triton
        Handler->>Cache: Store result
    end

    Handler-->>RMQ: Publish result
    deactivate Handler

    par Send to client
        RMQ->>Client: Route to ai.inference.results
    and Archive result
        RMQ->>Archive: Store for analysis
    end

    Note over Client,Archive: Typical latency 100-500ms
```

## 3. Event-Triggered Automation Flow

```mermaid
sequenceDiagram
    participant Device as IoT Device
    participant HA as Home Assistant
    participant RMQ as RabbitMQ
    participant Auto as Automation Service
    participant AI as AI Decision Service
    participant Executor as Action Executor

    Device->>HA: Trigger event
    activate HA
    HA->>RMQ: Publish to home.events
    deactivate HA

    par Process automation
        RMQ->>Auto: Route to automation.triggers
        activate Auto
        Auto->>RMQ: Request decision
        deactivate Auto

        RMQ->>AI: Route to ai.decision
        activate AI
        AI-->>RMQ: Return decision
        deactivate AI
    end

    RMQ->>Executor: Route to automation.actions
    activate Executor
    Executor->>HA: Execute action
    HA-->>Executor: Confirm execution
    Executor-->>RMQ: Publish result
    deactivate Executor

    Note over Device,Executor: Automation completes in < 1 second
```

## 4. Alert Generation and Notification Flow

```mermaid
sequenceDiagram
    participant Source as Alert Source
    participant RMQ as RabbitMQ
    participant Proc as Alert Processor
    participant AI as AI Enhancement
    participant Alert as Alert Manager
    participant Notify as Notification Service
    participant User as End User

    Source->>RMQ: Publish alert event
    activate RMQ
    RMQ->>Proc: Route to alerts.raw
    deactivate RMQ

    activate Proc
    Proc->>AI: Request context enhancement
    activate AI
    AI-->>Proc: Return enhanced alert
    deactivate AI
    Proc->>RMQ: Publish enriched alert
    deactivate Proc

    RMQ->>Alert: Route to alerts.processed
    activate Alert

    par Alert Actions
        Alert->>Notify: Send notification request
        activate Notify
        Notify-->>User: Push notification
        Notify-->>Alert: Confirm delivery
        deactivate Notify
    and
        Alert->>RMQ: Publish to alerts.archived
    end
    deactivate Alert

    Note over Source,User: Critical alerts delivered in < 5 seconds
```

## Message Flow Notes

1. **Data Flow Principles**
   - All messages are JSON formatted
   - Messages include timestamps and correlation IDs
   - Failed messages are routed to dead letter queues
   - All flows support retry mechanisms

2. **Performance Targets**
   - Sensor data processing: < 2 seconds
   - AI inference: < 500ms (cached), < 2 seconds (uncached)
   - Automation triggers: < 1 second
   - Critical alerts: < 5 seconds

3. **Error Handling**
   - All services implement circuit breakers
   - Failed messages are retried 3 times
   - Critical errors trigger system alerts
   - All errors are logged and monitored

4. **Scalability**
   - Services are horizontally scalable
   - Message batching for high-volume flows
   - Caching for repeated operations
   - Load balancing across service instances
