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

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.BDDMockito.mock;
import static org.mockito.BDDMockito.verify;

import java.util.Collections;

import javax.sql.DataSource;

import org.junit.jupiter.api.Test;
import org.springframework.core.env.Environment;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

class ControlledFaultsTests {

	@Test
	void noFaultDoesNotChangeRequestOrVisitBehavior() {
		ControlledFaults faults = createFaults("");

		faults.applyRequestFault("/vets.html");
		faults.failVisitWriteIfSelected();

		assertThat(ReflectionTestUtils.getField(faults, "retainedHeap")).asList().isEmpty();
	}

	@Test
	void slowQueryRunsWithinOwnerSearch() {
		JdbcTemplate jdbcTemplate = mock(JdbcTemplate.class);
		ControlledFaults faults = createFaults("slow-query", jdbcTemplate);

		faults.delayOwnerSearchIfSelected();

		verify(jdbcTemplate).execute("SELECT pg_sleep(0.5)");
	}

	@Test
	void oupsIsNotCountedAsAnInjectedFault() throws Exception {
		JdbcTemplate jdbcTemplate = mock(JdbcTemplate.class);
		ControlledFaults faults = createFaults("slow-query", jdbcTemplate);
		MockHttpServletRequest request = new MockHttpServletRequest("GET", "/oups");

		assertThat(faults.preHandle(request, new MockHttpServletResponse(), new Object())).isTrue();
		org.mockito.Mockito.verifyNoInteractions(jdbcTemplate);
	}

	@Test
	void visitWriteFailsOnlyWhenConfigured() {
		ControlledFaults faults = createFaults("visit-write");

		assertThatThrownBy(faults::failVisitWriteIfSelected).isInstanceOf(IllegalStateException.class)
			.hasMessageContaining("FAULT=visit-write");
	}

	@Test
	void heapLeakRetainsOneMegabyteOnlyForVetPage() {
		ControlledFaults faults = createFaults("heap-leak");

		faults.applyRequestFault("/");
		faults.applyRequestFault("/vets.html");

		Object retainedHeap = ReflectionTestUtils.getField(faults, "retainedHeap");
		assertThat(retainedHeap).asList().hasSize(1);
		assertThat(((byte[]) ((java.util.List<?>) retainedHeap).get(0))).hasSize(1024 * 1024);
	}

	@Test
	void unknownFaultIsRejectedAtStartup() {
		ControlledFaults faults = createFaults("typo");

		assertThatThrownBy(() -> faults.run(null)).isInstanceOf(IllegalArgumentException.class)
			.hasMessageContaining("Unknown FAULT value");
	}

	private ControlledFaults createFaults(String fault) {
		return createFaults(fault, mock(JdbcTemplate.class));
	}

	private ControlledFaults createFaults(String fault, JdbcTemplate jdbcTemplate) {
		Environment environment = mock(Environment.class);
		org.mockito.BDDMockito.given(environment.getProperty("FAULT", "")).willReturn(fault);
		org.mockito.BDDMockito.given(environment.getProperty("FAULT_LATENCY_MS", Long.class, 400L)).willReturn(0L);
		org.mockito.BDDMockito.given(environment.getProperty("FAULT_VISIT_FAILURE_PERCENT", Integer.class, 30))
			.willReturn(100);
		DataSource dataSource = mock(DataSource.class);
		ControlledFaults faults = new ControlledFaults(environment, dataSource);
		ReflectionTestUtils.setField(faults, "jdbcTemplate", jdbcTemplate);
		return faults;
	}

}
