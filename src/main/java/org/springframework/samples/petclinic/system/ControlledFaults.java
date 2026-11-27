/*
 * Copyright 2012-2025 the original author or authors.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.springframework.samples.petclinic.system;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ThreadLocalRandom;

import javax.sql.DataSource;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.core.env.Environment;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.servlet.HandlerInterceptor;

public class ControlledFaults implements ApplicationRunner, HandlerInterceptor {

	private static final Logger logger = LoggerFactory.getLogger(ControlledFaults.class);

	private static final int HEAP_ALLOCATION_BYTES = 1024 * 1024;

	private static final String LATENCY_START_ATTRIBUTE = ControlledFaults.class.getName() + ".latencyStart";

	private final Environment environment;

	private final JdbcTemplate jdbcTemplate;

	private static final List<byte[]> retainedHeap = new CopyOnWriteArrayList<>();

	private final String fault;

	public ControlledFaults(Environment environment, DataSource dataSource) {
		this.environment = environment;
		this.jdbcTemplate = dataSource == null ? null : new JdbcTemplate(dataSource);
		this.fault = environment.getProperty("FAULT", "").trim().toLowerCase();
	}

	@Override
	public void run(ApplicationArguments args) {
		if (this.fault.isEmpty()) {
			logger.info("FAULT=none");
			return;
		}
		if (!List.of("latency", "slow-query", "visit-write", "heap-leak").contains(this.fault)) {
			throw new IllegalArgumentException("Unknown FAULT value: " + this.fault);
		}
		logger.warn("FAULT={} is active", this.fault);
	}

	public void applyRequestFault(String path) {
		switch (this.fault) {
			case "heap-leak" -> growHeap(path);
			default -> {
			}
		}
	}

	public String activeFault() {
		return this.fault.isEmpty() ? "none" : this.fault;
	}

	public void delayOwnerSearchIfSelected() {
		if ("slow-query".equals(this.fault)) {
			if (this.jdbcTemplate == null) {
				throw new IllegalStateException("FAULT=slow-query requires a DataSource");
			}
			this.jdbcTemplate.execute("SELECT pg_sleep(0.5)");
		}
	}

	public void failVisitWriteIfSelected() {
		int failurePercent = this.environment.getProperty("FAULT_VISIT_FAILURE_PERCENT", Integer.class, 30);
		if ("visit-write".equals(this.fault) && ThreadLocalRandom.current().nextInt(100) < failurePercent) {
			logger.error("FAULT=visit-write: simulated visit persistence failure");
			throw new IllegalStateException("FAULT=visit-write: simulated visit persistence failure");
		}
	}

	@Override
	public boolean preHandle(jakarta.servlet.http.HttpServletRequest request,
			jakarta.servlet.http.HttpServletResponse response, Object handler) {
		String path = request.getRequestURI();
		if (path.startsWith("/actuator") || path.startsWith("/webjars") || path.startsWith("/resources")
				|| path.equals("/oups")) {
			return true;
		}
		if ("latency".equals(this.fault)) {
			request.setAttribute(LATENCY_START_ATTRIBUTE, System.nanoTime());
		}
		applyRequestFault(path);
		return true;
	}

	@Override
	public void afterCompletion(jakarta.servlet.http.HttpServletRequest request,
			jakarta.servlet.http.HttpServletResponse response, Object handler, Exception exception) {
		Object startedAt = request.getAttribute(LATENCY_START_ATTRIBUTE);
		if (startedAt instanceof Long startNanos) {
			long extraNanos = (System.nanoTime() - startNanos) * 2 / 5;
			try {
				Thread.sleep(extraNanos / 1_000_000, (int) (extraNanos % 1_000_000));
			}
			catch (InterruptedException interrupted) {
				Thread.currentThread().interrupt();
				logger.warn("Interrupted during simulated latency", interrupted);
			}
		}
	}

	private void growHeap(String path) {
		if ("/vets.html".equals(path)) {
			this.retainedHeap.add(new byte[HEAP_ALLOCATION_BYTES]);
		}
	}

}
