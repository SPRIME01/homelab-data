import moment from 'moment';

export class StateHistoryAnalyzer {
  analyze(history: any[], type: string) {
    switch (type) {
      case 'stateChanges':
        return this.analyzeStateChanges(history);
      case 'duration':
        return this.analyzeDuration(history);
      case 'pattern':
        return this.detectPatterns(history);
      default:
        throw new Error(`Unknown analysis type: ${type}`);
    }
  }

  private analyzeStateChanges(history: any[]) {
    return history.map(entityHistory => ({
      entity_id: entityHistory[0]?.entity_id,
      changes: entityHistory.map((state: any, index: number, array: any[]) => {
        const previousState = array[index - 1];
        return {
          from: previousState?.state,
          to: state.state,
          duration: previousState
            ? moment(state.last_updated).diff(moment(previousState.last_updated), 'seconds')
            : 0,
          timestamp: state.last_updated,
        };
      }).filter((change: any) => change.from !== change.to),
    }));
  }

  private analyzeDuration(history: any[]) {
    return history.map(entityHistory => {
      const statesDuration: { [key: string]: number } = {};

      entityHistory.forEach((state: any, index: number, array: any[]) => {
        const duration = index < array.length - 1
          ? moment(array[index + 1].last_updated).diff(moment(state.last_updated), 'seconds')
          : 0;

        statesDuration[state.state] = (statesDuration[state.state] || 0) + duration;
      });

      return {
        entity_id: entityHistory[0]?.entity_id,
        durations: statesDuration,
        total_duration: Object.values(statesDuration).reduce((a, b) => a + b, 0),
      };
    });
  }

  private detectPatterns(history: any[]) {
    return history.map(entityHistory => {
      const patterns = [];
      let currentPattern = [];

      entityHistory.forEach((state: any, index: number, array: any[]) => {
        currentPattern.push(state.state);

        if (currentPattern.length >= 3) {
          // Look for repeating sequences
          const sequence = this.findRepeatingSequence(currentPattern);
          if (sequence) {
            patterns.push({
              sequence,
              start_time: array[index - sequence.length + 1].last_updated,
              end_time: state.last_updated,
            });
          }
        }
      });

      return {
        entity_id: entityHistory[0]?.entity_id,
        patterns,
      };
    });
  }

  private findRepeatingSequence(states: string[]): string[] | null {
    for (let length = 2; length <= states.length / 2; length++) {
      const sequence = states.slice(-length);
      const previousSequence = states.slice(-length * 2, -length);

      if (JSON.stringify(sequence) === JSON.stringify(previousSequence)) {
        return sequence;
      }
    }
    return null;
  }
}
