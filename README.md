flowchart LR
    %% External Systems
    TV[TradingView<br/>Alerts]:::external
    DHAN[DHAN Broker API]:::external
    TG[Telegram API]:::external
    NG[ngrok<br/>Tunnel API]:::external

    %% User
    U[User / Admin]:::user

    %% Application
    subgraph APP["FastAPI Trading Bridge"]
        W[Webhook API<br/>/webhook]
        S[Settings UI<br/>/settings]
        T[Test APIs<br/>/api/test-dhan]
        H[Health Check<br/>/health]

        subgraph CORE["Trading Engine"]
            N[Symbol Normalizer]
            E[Expiry Resolver]
            I[Instrument Lookup<br/>(CSV Cache)]
            L[State Manager<br/>(JSON File)]
            R[Risk & Quantity<br/>Calculator]
            O[Order Executor]
        end
    end

    %% Storage
    CSV[(Instruments CSV)]:::storage
    STATE[(State File<br/>tv_bridge_state.json)]:::storage
    ENV[(.env Config)]:::storage

    %% Flows
    TV -->|Alert JSON| W
    W --> N --> E --> I
    I --> R --> O
    O -->|Place Orders| DHAN
    O -->|Notifications| TG

    O --> L
    L --> STATE

    I --> CSV

    U --> S
    S -->|Update Token / Expiry| ENV

    U --> T --> DHAN
    U --> H

    APP --> NG

    %% Styles
    classDef external fill:#f5f5f5,stroke:#333,stroke-width:1px;
    classDef storage fill:#e3f2fd,stroke:#1e88e5,stroke-width:1px;
    classDef user fill:#fff3e0,stroke:#ef6c00,stroke-width:1px;
