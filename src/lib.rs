#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SetupStep { #[default] Welcome, Network, UserAccount, Themes, Completed }

#[derive(Debug, Default)]
pub struct SetupAssistant {
    pub current_step: SetupStep,
}

impl SetupAssistant {
    pub fn advance(&mut self) {
        self.current_step = match self.current_step {
            SetupStep::Welcome => SetupStep::Network,
            SetupStep::Network => SetupStep::UserAccount,
            SetupStep::UserAccount => SetupStep::Themes,
            SetupStep::Themes => SetupStep::Completed,
            SetupStep::Completed => SetupStep::Completed,
        };
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn test_setup_assistant_flow_aaa() {
        // Arrange
        let mut setup = SetupAssistant::default();
        assert_eq!(setup.current_step, SetupStep::Welcome);
        // Act
        setup.advance();
        setup.advance();
        // Assert
        assert_eq!(setup.current_step, SetupStep::UserAccount);
    }
}
