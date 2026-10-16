# Fabric Desktop: Bringing the Power BI Model to Data Engineering

One of the biggest reasons Power BI succeeded was simple: developers could build locally, take advantage of their own computing resources, and deploy to the cloud. This gave developers the freedom to build, test, and iterate without consuming cloud resources during development.

![Power BI success: build locally in Power BI Desktop, then deploy and run in the cloud on the Power BI Service](power-bi-model.png)

Fabric Desktop brings that same philosophy to Microsoft Fabric.

Thanks to OneLake and open Delta Lake formats, developers can now go a step further. They can build and execute data engineering workloads using local compute resources while reading and writing data directly to OneLake. That data is immediately available to Microsoft Fabric workloads, including Warehouses, Lakehouses, Power BI, and AI.

![Fabric Desktop: build locally, run in your infrastructure, read and write directly to OneLake, and consume in every Microsoft Fabric workload](fabric-desktop-model.png)

The result is a new approach to Fabric development:

**Build locally. Execute anywhere. Store once in OneLake. Consume everywhere in Fabric.**

Fabric Desktop isn't replacing Microsoft Fabric. It's extending it, giving developers and organizations greater flexibility, control over compute costs, and the freedom to use their own infrastructure while taking full advantage of the Fabric ecosystem.

## Read next

- [README](https://github.com/KratosDataGod/fabric-desktop/blob/main/README.md): what Fabric Desktop does today and how to set it up.
- [Architecture](architecture.md): how the app, the local engine and OneLake fit together.
- [Headless agent](agent.md): running the same workloads unattended on your own servers.
- [Enterprise roadmap](enterprise-roadmap.md): where local execution goes next.
