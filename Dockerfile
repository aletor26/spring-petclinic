# Etapa 1: compilar el jar con Maven (no hace falta Java instalado en la máquina)
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /app
COPY pom.xml .
COPY .mvn .mvn
RUN mvn -B -q dependency:go-offline -DskipTests || true
COPY src src
RUN mvn -B -q package -DskipTests -Dcheckstyle.skip -Dspring-javaformat.skip=true

# Etapa 2: imagen liviana solo con el JRE
FROM eclipse-temurin:17-jre
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/target/*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
